"""Context selection, compaction and durable agent memory."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.domain import EventType, TaskView
from minion.events import EventBus, EventStore
from minion.repositories import CheckpointRepository, SessionRepository
from minion.runtime.intelligence import RepositoryIntelligence
from minion.runtime.router import AgentProfile


SYSTEM_PROMPT = """You are a production software-engineering agent in an isolated workspace.

Rules:
1. Inspect before editing. Never invent repository structure.
2. User instructions/constraints override older plans.
3. Maintain a concrete plan with update_plan when the approach changes materially.
4. Use repository/context tools for evidence.
5. Run relevant tests/linters after changes.
6. Use git_diff before declaring completion.
7. Create checkpoint commits after meaningful stable progress.
8. For multi-repository work, reason about dependency and rollout order.
9. Declare completion only after verification; then make no more tool calls.
"""


class ContextManager:
    def __init__(
        self,
        settings: Settings,
        db: AsyncSession,
        bus: EventBus,
        intelligence: RepositoryIntelligence,
        profile: AgentProfile,
    ):
        self.settings = settings
        self.db = db
        self.events = EventStore(db, bus)
        self.sessions = SessionRepository(db)
        self.checkpoints = CheckpointRepository(db)
        self.intelligence = intelligence
        self.profile = profile

    async def set_plan(self, session_id: str, plan: list[str]) -> None:
        await self.sessions.update_memory(session_id, current_plan=plan)

    async def save_checkpoint(
        self,
        task: TaskView,
        repo_name: str,
        base_branch: str,
        commit_sha: str,
        binary_patch: str,
    ) -> None:
        await self.checkpoints.save(
            task_id=task.id,
            session_id=task.session_id,
            repo_name=repo_name,
            base_branch=base_branch,
            commit_sha=commit_sha,
            binary_patch=binary_patch,
        )
        await self.events.append(
            task.id,
            task.session_id,
            EventType.CHECKPOINT_CREATED,
            {
                "repo": repo_name,
                "commit_sha": commit_sha,
                "patch_bytes": len(binary_patch.encode()),
            },
        )

    async def maybe_compact(self, task: TaskView) -> None:
        session = await self.sessions.get(task.session_id)
        if session is None:
            raise KeyError(task.session_id)
        threshold = self.settings.context_compact_every_events
        delta = session.last_event_sequence - session.last_compacted_sequence
        if threshold <= 0 or delta < threshold:
            return

        start = max(0, session.last_event_sequence - 200)
        events = await self.events.list_after(task.id, start, limit=200)
        important = []
        for event in events:
            if event.type not in {
                EventType.USER_MESSAGE,
                EventType.AGENT_MESSAGE,
                EventType.TOOL_COMPLETED,
                EventType.TOOL_FAILED,
                EventType.CHECKPOINT_CREATED,
            }:
                continue
            payload = json.dumps(event.payload, ensure_ascii=False)
            important.append(f"#{event.sequence} {event.type.value}: {payload[:1200]}")

        summary = (
            "Durable execution summary (recent high-signal history):\n"
            + "\n".join(important[-40:])
        )
        await self.sessions.update_memory(
            task.session_id,
            summary=summary,
            last_compacted_sequence=session.last_event_sequence,
        )
        await self.events.append(
            task.id,
            task.session_id,
            EventType.CONTEXT_COMPACTED,
            {"through_sequence": session.last_event_sequence},
        )

    async def build_messages(self, task: TaskView) -> list[dict[str, Any]]:
        session = await self.sessions.get(task.session_id)
        if session is None:
            raise KeyError(task.session_id)

        first_sequence = max(
            0,
            session.last_event_sequence - self.settings.context_recent_events,
        )
        recent = await self.events.list_after(task.id, first_sequence)

        repositories = ", ".join(
            f"{repo.name or repo.url.split('/')[-1].removesuffix('.git')}@{repo.base_branch}"
            for repo in task.repositories
        )
        retrieval_query = " ".join(
            [task.instruction, *session.active_constraints[-10:]]
        )
        retrieved = await self.intelligence.retrieve(retrieval_query)

        state = {
            "task_id": task.id,
            "agent_profile": self.profile.name,
            "profile_guidance": self.profile.guidance,
            "repositories": repositories,
            "summary": session.summary,
            "current_plan": session.current_plan,
            "active_user_instructions": session.active_constraints,
        }

        history_lines: list[str] = []
        for event in recent:
            payload = json.dumps(event.payload, ensure_ascii=False)
            history_lines.append(
                f"#{event.sequence} {event.type.value}: {payload[:6000]}"
            )

        user = (
            f"ORIGINAL TASK:\n{task.instruction}\n\n"
            f"CURRENT DURABLE STATE:\n{json.dumps(state, indent=2)}\n\n"
            f"RETRIEVED REPOSITORY CONTEXT:\n{retrieved or 'No indexed context yet.'}\n\n"
            f"RECENT EVENTS:\n" + "\n".join(history_lines)
        )
        if len(user) > self.settings.context_max_chars:
            user = user[: self.settings.context_max_chars] + "\n...[context truncated]"

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
