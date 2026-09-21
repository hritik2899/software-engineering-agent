"""Context construction for each model step.

Memory is everything persisted about a task. Context is the intentionally small
subset sent to the model *now*. Older history is represented by the durable
session summary; recent high-signal events remain verbatim.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.events import EventBus, EventStore
from minion.repositories import SessionRepository
from minion.domain import TaskView


SYSTEM_PROMPT = """You are a production software-engineering agent working in an isolated task workspace.

Goal: complete the user's engineering task with the smallest correct change.

Rules:
1. Inspect before editing. Do not invent repository structure.
2. Respect active user constraints over older plans.
3. Use repository tools for evidence.
4. Run relevant tests/linters after changes.
5. Use git_diff before declaring completion.
6. Create checkpoint commits after meaningful stable progress.
7. If multiple repositories are involved, reason about dependency/order explicitly.
8. When the task is complete and verified, respond with a concise completion summary and make no further tool calls.
"""


class ContextManager:
    def __init__(self, settings: Settings, db: AsyncSession, bus: EventBus):
        self.settings = settings
        self.db = db
        self.events = EventStore(db, bus)
        self.sessions = SessionRepository(db)

    async def build_messages(self, task: TaskView) -> list[dict[str, Any]]:
        session = await self.sessions.get(task.session_id)
        if not session:
            raise KeyError(task.session_id)
        recent = await self.events.list_after(task.id, max(0, session.last_event_sequence - self.settings.context_recent_events))

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
            user = user[: self.settings.context_max_chars] + "\n...[context truncated]"
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
