"""Autonomous coding-agent loop with runtime-enforced control and completion gates."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from minion.domain import EventType, TaskView
from minion.events import EventStore
from minion.runtime.context import ContextManager
from minion.runtime.llm import LLMClient, ModelTurn
from minion.runtime.router import route_agent
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
        should_cancel: Callable[[], Awaitable[bool]],
        should_pause: Callable[[], Awaitable[bool]],
    ):
        self.llm = llm
        self.tools = tools
        self.context = context
        self.events = events
        self.max_steps = max_steps
        self.should_cancel = should_cancel
        self.should_pause = should_pause

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _model_turn(self, messages: list[dict[str, Any]]) -> ModelTurn:
        return await self.llm.complete(messages, self.tools.schemas)

    async def _honor_control_state(self) -> None:
        if await self.should_cancel():
            raise asyncio.CancelledError
        while await self.should_pause():
            await asyncio.sleep(0.5)
            if await self.should_cancel():
                raise asyncio.CancelledError

    async def run(self, task: TaskView) -> dict[str, Any]:
        self.context.profile = route_agent(task.instruction)
        changed_code = False
        ran_verification = False
        inspected_git = False
        checkpointed = False

        async def persist_plan(plan: list[str]) -> None:
            await self.context.set_plan(task.session_id, plan)

        async def persist_checkpoint(
            repo_name: str,
            base_branch: str,
            commit_sha: str,
            binary_patch: str,
        ) -> None:
            await self.context.save_checkpoint(
                task,
                repo_name,
                base_branch,
                commit_sha,
                binary_patch,
            )

        self.tools.update_plan_callback = persist_plan
        self.tools.checkpoint_callback = persist_checkpoint

        for step in range(1, self.max_steps + 1):
            await self._honor_control_state()
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
                    {"content": turn.content, "step": step},
                )

            if not turn.tool_calls:
                if changed_code and not (
                    ran_verification and inspected_git and checkpointed
                ):
                    await self.events.append(
                        task.id,
                        task.session_id,
                        EventType.AGENT_MESSAGE,
                        {
                            "content": (
                                "Completion rejected by runtime gate: edited tasks "
                                "must run verification, inspect Git, and create a "
                                "durable checkpoint before completion."
                            ),
                            "step": step,
                        },
                    )
                    continue
                return {"summary": turn.content, "steps": step}

            for call in turn.tool_calls:
                await self._honor_control_state()
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
                result = await self.tools.execute(call.name, call.arguments)
                if result.ok:
                    changed_code = changed_code or call.name == "write_file"
                    ran_verification = ran_verification or call.name == "run_command"
                    inspected_git = inspected_git or call.name in {
                        "git_diff",
                        "git_status",
                        "checkpoint",
                    }
                    checkpointed = checkpointed or call.name == "checkpoint"
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

        raise RuntimeError(f"agent exceeded max steps ({self.max_steps})")
