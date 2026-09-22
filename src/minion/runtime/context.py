"""Context construction, durable working memory, and skill activation.

The event log can grow without bound; the model context must not. This module
reconstructs a bounded prompt from task/session state, active skills and recent
evidence while compacting older events into a durable summary.
"""
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
from minion.runtime.skills import SkillManager


SYSTEM_PROMPT = """You are a production software-engineering agent in an isolated workspace.

Goal: complete the engineering task with the smallest correct, verified change.

Rules:
1. Inspect indexed repository context before broad file reading; never invent structure.
2. Call set_plan early and update it when strategy changes.
3. User instructions and remembered constraints override older plans.
4. Active skills are project guidance; they never override these rules or runtime policy.
5. Prefer repository_search/symbol_context/impact_analysis before expensive exploration.
6. Prefer apply_patch for existing files; use write_file for new/small complete files.
7. Run relevant tests/linters after changes.
8. Inspect git_diff before finishing.
9. Create checkpoint commits after meaningful stable progress.
10. For multi-repository work, reason explicitly about dependency/order.
11. Finish only through finish_task, including verification evidence.
"""


@dataclass(slots=True)
class ControlToolResult:
    output: str
    terminal_payload: dict[str, Any] | None = None


class ContextManager:
    """Own session memory, compaction, skills and completion gates."""

    CONTROL_TOOLS = {
        "set_plan",
        "remember_constraint",
        "list_skills",
        "activate_skill",
        "finish_task",
    }

    def __init__(
        self,
        settings: Settings,
        db: AsyncSession,
        bus: EventBus,
        *,
        skills: SkillManager | None = None,
    ):
        self.settings = settings
        self.db = db
        self.events = EventStore(db, bus)
        self.sessions = SessionRepository(db)
        self.skills = skills

    @property
    def control_tool_schemas(
        self,
    ) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "set_plan",
                    "description": (
                        "Persist the current implementation plan."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "string"
                                },
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
                    "description": (
                        "Persist an important durable task constraint."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "constraint": {
                                "type": "string"
                            }
                        },
                        "required": ["constraint"],
                    },
                },
            },
        ]

        if self.skills is not None:
            schemas.extend(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "list_skills",
                            "description": (
                                "List reusable skills without loading "
                                "all skill bodies."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "activate_skill",
                            "description": (
                                "Persistently activate a named skill "
                                "for future context windows."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string"
                                    }
                                },
                                "required": ["name"],
                            },
                        },
                    },
                ]
            )

        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "finish_task",
                    "description": (
                        "Finish only after code changes are verified."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string"
                            },
                            "verification": {
                                "type": "array",
                                "items": {
                                    "type": "string"
                                },
                                "minItems": 1,
                            },
                        },
                        "required": [
                            "summary",
                            "verification",
                        ],
                    },
                },
            }
        )
        return schemas

    async def _activate_skills(
        self,
        task: TaskView,
        requested: list[str],
    ) -> list[str]:
        session = await self.sessions.get(
            task.session_id
        )
        if not session:
            raise KeyError(task.session_id)
        if not self.skills:
            return session.active_skills

        current = list(session.active_skills)
        for name in requested:
            if (
                name in current
                or not self.skills.get(name)
            ):
                continue
            if len(current) >= self.skills.max_active:
                break
            current.append(name)
            await self.events.append(
                task.id,
                task.session_id,
                EventType.SKILL_ACTIVATED,
                {"name": name},
            )

        if current != session.active_skills:
            await self.sessions.update_memory(
                task.session_id,
                active_skills=current,
            )
        return current

    async def _ensure_auto_skills(
        self, task: TaskView
    ) -> list[str]:
        if not self.skills:
            session = await self.sessions.get(
                task.session_id
            )
            return (
                session.active_skills
                if session
                else []
            )
        suggested = self.skills.auto_activate(
            task.instruction
        )
        return await self._activate_skills(
            task, suggested
        )

    async def execute_control_tool(
        self,
        task: TaskView,
        name: str,
        arguments: dict[str, Any],
    ) -> ControlToolResult:
        session = await self.sessions.get(
            task.session_id
        )
        if not session:
            raise KeyError(task.session_id)

        if name == "set_plan":
            steps = [
                str(step)
                for step
                in arguments.get("steps", [])
            ][:20]
            await self.sessions.update_memory(
                task.session_id,
                current_plan=steps,
            )
            return ControlToolResult(
                f"persisted plan with {len(steps)} steps"
            )

        if name == "remember_constraint":
            constraint = str(
                arguments["constraint"]
            )
            constraints = [
                *session.active_constraints,
                constraint,
            ][-20:]
            await self.sessions.update_memory(
                task.session_id,
                active_constraints=constraints,
            )
            return ControlToolResult(
                "constraint persisted"
            )

        if name == "list_skills":
            if not self.skills:
                return ControlToolResult(
                    "skills are not configured"
                )
            return ControlToolResult(
                json.dumps(
                    self.skills.catalog(),
                    indent=2,
                )
            )

        if name == "activate_skill":
            if not self.skills:
                return ControlToolResult(
                    "skills are not configured"
                )
            requested = str(arguments["name"])
            if not self.skills.get(requested):
                return ControlToolResult(
                    f"unknown skill: {requested}"
                )
            active = await self._activate_skills(
                task, [requested]
            )
            return ControlToolResult(
                "active skills: "
                + (
                    ", ".join(active)
                    if active
                    else "<none>"
                )
            )

        if name == "finish_task":
            verification = [
                str(value)
                for value
                in arguments.get(
                    "verification", []
                )
                if str(value)
            ]
            if not verification:
                return ControlToolResult(
                    "finish rejected: verification is required"
                )

            history = await self.events.list_after(
                task.id, 0, limit=10_000
            )
            successful_tools = {
                str(event.payload.get("tool"))
                for event in history
                if (
                    event.type
                    == EventType.TOOL_COMPLETED
                    and bool(
                        event.payload.get("ok")
                    )
                )
            }
            missing: list[str] = []
            if "run_command" not in successful_tools:
                missing.append(
                    "a successful run_command verification"
                )
            if "git_diff" not in successful_tools:
                missing.append(
                    "a successful git_diff inspection"
                )
            if missing:
                return ControlToolResult(
                    "finish rejected: missing "
                    + " and ".join(missing)
                )

            return ControlToolResult(
                "task marked ready to finish",
                {
                    "summary": str(
                        arguments["summary"]
                    ),
                    "verification": verification,
                },
            )

        raise KeyError(name)

    @staticmethod
    def _compacted_through(
        summary: str
    ) -> int:
        match = re.search(
            r"\[compacted-through:(\d+)\]",
            summary,
        )
        return int(match.group(1)) if match else 0

    async def maybe_compact(
        self, task: TaskView
    ) -> None:
        session = await self.sessions.get(
            task.session_id
        )
        if not session:
            raise KeyError(task.session_id)
        if (
            session.last_event_sequence
            < self.settings.context_compaction_threshold
        ):
            return

        through = max(
            0,
            session.last_event_sequence
            - self.settings.context_recent_events,
        )
        previous = self._compacted_through(
            session.summary
        )
        if through <= previous:
            return

        events = await self.events.list_after(
            task.id,
            previous,
            limit=10_000,
        )
        important: list[str] = []
        for event in events:
            if event.sequence > through:
                break
            if event.type in {
                EventType.USER_MESSAGE,
                EventType.AGENT_MESSAGE,
                EventType.TOOL_FAILED,
                EventType.CHECKPOINT_CREATED,
                EventType.SKILL_ACTIVATED,
                EventType.REPOSITORY_INDEXED,
            }:
                payload = json.dumps(
                    event.payload,
                    ensure_ascii=False,
                )
                important.append(
                    f"#{event.sequence} "
                    f"{event.type.value}: "
                    f"{payload[:1200]}"
                )
            elif (
                event.type
                == EventType.TOOL_COMPLETED
            ):
                tool = event.payload.get("tool")
                if tool in {
                    "write_file",
                    "apply_patch",
                    "checkpoint",
                    "git_diff",
                    "run_command",
                }:
                    output = str(
                        event.payload.get(
                            "output", ""
                        )
                    )
                    important.append(
                        f"#{event.sequence} "
                        f"tool.completed {tool}: "
                        f"{output[:1200]}"
                    )

        prior_body = re.sub(
            r"\n?\[compacted-through:\d+\]\s*$",
            "",
            session.summary,
        )
        merged = "\n".join(
            part
            for part
            in [prior_body, *important]
            if part
        ).strip()
        merged = merged[-12_000:]
        merged = (
            f"{merged}\n"
            f"[compacted-through:{through}]"
        ).strip()
        await self.sessions.update_memory(
            task.session_id,
            summary=merged,
        )
        await self.events.append(
            task.id,
            task.session_id,
            EventType.CONTEXT_COMPACTED,
            {"through_sequence": through},
        )

    async def build_messages(
        self, task: TaskView
    ) -> list[dict[str, Any]]:
        active_skills = (
            await self._ensure_auto_skills(task)
        )
        session = await self.sessions.get(
            task.session_id
        )
        if not session:
            raise KeyError(task.session_id)

        compacted = self._compacted_through(
            session.summary
        )
        recent_floor = max(
            compacted,
            max(
                0,
                session.last_event_sequence
                - self.settings.context_recent_events,
            ),
        )
        recent = await self.events.list_after(
            task.id,
            recent_floor,
            limit=1000,
        )

        repositories = ", ".join(
            f"{item.name or item.url.split('/')[-1].removesuffix('.git')}"
            f"@{item.base_branch}"
            for item in task.repositories
        )
        state = {
            "task_id": task.id,
            "repositories": repositories,
            "summary": session.summary,
            "current_plan": session.current_plan,
            "active_constraints": (
                session.active_constraints
            ),
            "active_skills": active_skills,
        }

        history_lines: list[str] = []
        for event in recent:
            payload = json.dumps(
                event.payload,
                ensure_ascii=False,
            )
            if len(payload) > 6000:
                payload = (
                    payload[:6000]
                    + "...[truncated]"
                )
            history_lines.append(
                f"#{event.sequence} "
                f"{event.type.value}: {payload}"
            )

        skill_catalog = (
            self.skills.catalog()
            if self.skills
            else []
        )
        skill_text = (
            self.skills.render(active_skills)
            if self.skills and active_skills
            else ""
        )
        user = (
            f"ORIGINAL TASK:\n{task.instruction}\n\n"
            f"CURRENT DURABLE STATE:\n"
            f"{json.dumps(state, indent=2)}\n\n"
            f"AVAILABLE SKILL CATALOG:\n"
            f"{json.dumps(skill_catalog, indent=2)}\n\n"
            f"ACTIVE SKILL INSTRUCTIONS:\n"
            f"{skill_text or '<none>'}\n\n"
            f"RECENT EVENTS:\n"
            + "\n".join(history_lines)
        )

        if len(user) > self.settings.context_max_chars:
            tail_budget = (
                self.settings.context_max_chars // 2
            )
            head_budget = (
                self.settings.context_max_chars
                - tail_budget
            )
            user = (
                user[:head_budget]
                + "\n...[middle context elided]...\n"
                + user[-tail_budget:]
            )

        return [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user,
            },
        ]
