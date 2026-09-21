"""Context construction and durable working memory."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.domain import EventType, TaskView
from minion.events import EventBus, EventStore
from minion.repositories import SessionRepository


SYSTEM_PROMPT = """You are a production software-engineering agent in an isolated workspace.

Goal: complete the engineering task with the smallest correct, verified change.

Rules:
1. Inspect before editing; never invent repository structure.
2. Call set_plan early and update it when strategy changes.
3. User instructions and remembered constraints override older plans.
4. Use repository tools for evidence.
5. Run relevant tests/linters after changes.
6. Inspect git_diff before finishing.
7. Create checkpoint commits after meaningful stable progress.
8. For multi-repository work, reason explicitly about dependency/order.
9. Finish only through finish_task, including verification evidence.
"""


@dataclass(slots=True)
class ControlToolResult:
    output: str
    terminal_payload: dict[str, Any] | None = None


class ContextManager:
    CONTROL_TOOLS = {"set_plan", "remember_constraint", "finish_task"}

    def __init__(self, settings: Settings, db: AsyncSession, bus: EventBus):
        self.settings = settings
        self.db = db
        self.events = EventStore(db, bus)
        self.sessions = SessionRepository(db)

    @property
    def control_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "set_plan",
                    "description": "Persist the current implementation plan.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["steps"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remember_constraint",
                    "description": "Persist an important durable task constraint.",
                    "parameters": {
                        "type": "object",
                        "properties": {"constraint": {"type": "string"}},
                        "required": ["constraint"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish_task",
                    "description": "Finish only after code changes are verified.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "verification": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                        },
                        "required": ["summary", "verification"],
                    },
                },
            },
        ]

    async def execute_control_tool(
        self, task: TaskView, name: str, arguments: dict[str, Any]
    ) -> ControlToolResult:
        session = await self.sessions.get(task.session_id)
        if not session:
            raise KeyError(task.session_id)

        if name == "set_plan":
            steps = [str(step) for step in arguments.get("steps", [])][:20]
            await self.sessions.update_memory(task.session_id, current_plan=steps)
            return ControlToolResult(f"persisted plan with {len(steps)} steps")

        if name == "remember_constraint":
            constraint = str(arguments["constraint"])
            constraints = [*session.active_constraints, constraint][-20:]
            await self.sessions.update_memory(
                task.session_id, active_constraints=constraints
            )
            return ControlToolResult("constraint persisted")

        if name == "finish_task":
            verification = [
                str(v) for v in arguments.get("verification", []) if str(v)
            ]
            if not verification:
                return ControlToolResult("finish rejected: verification is required")

            # Do not trust a model's textual claim that it verified the work.
            # Require evidence in the durable event log from the actual runtime.
            history = await self.events.list_after(task.id, 0, limit=10_000)
            successful_tools = {
                str(event.payload.get("tool"))
                for event in history
                if event.type == EventType.TOOL_COMPLETED
                and bool(event.payload.get("ok"))
            }
            missing: list[str] = []
            if "run_command" not in successful_tools:
                missing.append("a successful run_command verification")
            if "git_diff" not in successful_tools:
                missing.append("a successful git_diff inspection")
            if missing:
                return ControlToolResult(
                    "finish rejected: missing " + " and ".join(missing)
                )

            payload = {
                "summary": str(arguments["summary"]),
                "verification": verification,
            }
            return ControlToolResult("task marked ready to finish", payload)

        raise KeyError(name)

    @staticmethod
    def _compacted_through(summary: str) -> int:
        match = re.search(r"\[compacted-through:(\d+)\]", summary)
        return int(match.group(1)) if match else 0

    async def maybe_compact(self, task: TaskView) -> None:
        session = await self.sessions.get(task.session_id)
        if not session:
            raise KeyError(task.session_id)
        if session.last_event_sequence < self.settings.context_compaction_threshold:
            return

        through = max(
            0, session.last_event_sequence - self.settings.context_recent_events
        )
        previous = self._compacted_through(session.summary)
        if through <= previous:
            return

        events = await self.events.list_after(task.id, previous, limit=10_000)
        important: list[str] = []
        for event in events:
            if event.sequence > through:
                break
            if event.type in {
                EventType.USER_MESSAGE,
                EventType.AGENT_MESSAGE,
                EventType.TOOL_FAILED,
                EventType.CHECKPOINT_CREATED,
            }:
                payload = json.dumps(event.payload, ensure_ascii=False)
                important.append(f"#{event.sequence} {event.type.value}: {payload[:1200]}")
            elif event.type == EventType.TOOL_COMPLETED:
                tool = event.payload.get("tool")
                if tool in {"write_file", "checkpoint", "git_diff", "run_command"}:
                    output = str(event.payload.get("output", ""))
                    important.append(
                        f"#{event.sequence} tool.completed {tool}: {output[:1200]}"
                    )

        prior_body = re.sub(r"\n?\[compacted-through:\d+\]\s*$", "", session.summary)
        merged = "\n".join(part for part in [prior_body, *important] if part).strip()
        merged = merged[-12_000:]
        merged = f"{merged}\n[compacted-through:{through}]".strip()
        await self.sessions.update_memory(task.session_id, summary=merged)
        await self.events.append(
            task.id,
            task.session_id,
            EventType.CONTEXT_COMPACTED,
            {"through_sequence": through},
        )

    async def build_messages(self, task: TaskView) -> list[dict[str, Any]]:
        session = await self.sessions.get(task.session_id)
        if not session:
            raise KeyError(task.session_id)

        compacted = self._compacted_through(session.summary)
        recent_floor = max(
            compacted,
            max(0, session.last_event_sequence - self.settings.context_recent_events),
        )
        recent = await self.events.list_after(task.id, recent_floor, limit=1000)

        repositories = ", ".join(
            f"{r.name or r.url.split('/')[-1].removesuffix('.git')}@{r.base_branch}"
            for r in task.repositories
        )
        state = {
            "task_id": task.id,
            "repositories": repositories,
            "summary": session.summary,
            "current_plan": session.current_plan,
            "active_constraints": session.active_constraints,
        }
        history_lines: list[str] = []
        for event in recent:
            payload = json.dumps(event.payload, ensure_ascii=False)
            if len(payload) > 6000:
                payload = payload[:6000] + "...[truncated]"
            history_lines.append(f"#{event.sequence} {event.type.value}: {payload}")

        user = (
            f"ORIGINAL TASK:\n{task.instruction}\n\n"
            f"CURRENT DURABLE STATE:\n{json.dumps(state, indent=2)}\n\n"
            f"RECENT EVENTS:\n" + "\n".join(history_lines)
        )
        if len(user) > self.settings.context_max_chars:
            tail_budget = self.settings.context_max_chars // 2
            head_budget = self.settings.context_max_chars - tail_budget
            user = (
                user[:head_budget]
                + "\n...[middle context elided]...\n"
                + user[-tail_budget:]
            )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
