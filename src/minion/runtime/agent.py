"""Autonomous reasoning/action loop.

The orchestrator owns task/environment lifecycle; this class owns the repeated
build-context -> model -> tools -> persisted-evidence loop inside one task.
Read-only tool batches can execute concurrently; mutations stay ordered.
"""
from __future__ import annotations

import asyncio
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from minion.domain import EventType, TaskView
from minion.events import EventStore
from minion.runtime.context import ContextManager
from minion.runtime.llm import LLMClient, ModelTurn, ToolCall
from minion.runtime.tools import ToolRegistry, ToolResult


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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _model_turn(
        self,
        messages: list[dict[str, Any]],
    ) -> ModelTurn:
        schemas = [
            *self.tools.schemas,
            *self.context.control_tool_schemas,
        ]
        return await self.llm.complete(messages, schemas)

    async def _announce_tool(
        self,
        task: TaskView,
        call: ToolCall,
    ) -> None:
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

    async def _record_external_result(
        self,
        task: TaskView,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        await self.events.append(
            task.id,
            task.session_id,
            (
                EventType.TOOL_COMPLETED
                if result.ok
                else EventType.TOOL_FAILED
            ),
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

    async def _run_external_call(
        self,
        task: TaskView,
        call: ToolCall,
    ) -> tuple[ToolCall, ToolResult]:
        result = await self.tools.execute(
            call.name,
            call.arguments,
        )
        await self._record_external_result(
            task,
            call,
            result,
        )
        return call, result

    async def _execute_turn_tools(
        self,
        task: TaskView,
        calls: list[ToolCall],
    ) -> dict[str, Any] | None:
        for call in calls:
            await self._announce_tool(task, call)

        all_external = all(
            call.name not in self.context.CONTROL_TOOLS
            for call in calls
        )
        parallel_batch = (
            len(calls) > 1
            and all_external
            and all(
                self.tools.is_parallel_safe(call.name)
                for call in calls
            )
        )
        if parallel_batch:
            await asyncio.gather(
                *(
                    self._run_external_call(task, call)
                    for call in calls
                )
            )
            return None

        for call in calls:
            if call.name in self.context.CONTROL_TOOLS:
                result = (
                    await self.context.execute_control_tool(
                        task,
                        call.name,
                        call.arguments,
                    )
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
                    return result.terminal_payload
                continue

            await self._run_external_call(task, call)

        return None

    async def run(
        self, task: TaskView
    ) -> dict[str, Any]:
        for step in range(1, self.max_steps + 1):
            await self.context.maybe_compact(task)
            await self.events.append(
                task.id,
                task.session_id,
                EventType.AGENT_STEP,
                {"step": step},
            )
            messages = await self.context.build_messages(task)
            turn = await self._model_turn(messages)

            if turn.content:
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.AGENT_MESSAGE,
                    {
                        "content": turn.content,
                        "step": step,
                    },
                )

            if not turn.tool_calls:
                await self.events.append(
                    task.id,
                    task.session_id,
                    EventType.AGENT_MESSAGE,
                    {
                        "content": (
                            "The model returned no tool call. "
                            "Continue working and finish explicitly "
                            "with finish_task after verification."
                        ),
                        "step": step,
                    },
                )
                continue

            terminal = await self._execute_turn_tools(
                task,
                turn.tool_calls,
            )
            if terminal is not None:
                return {
                    **terminal,
                    "steps": step,
                }

        raise RuntimeError(
            f"agent exceeded max steps ({self.max_steps})"
        )
