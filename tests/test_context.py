from pathlib import Path

import pytest

from minion.db import Database
from minion.domain import EventType, RepositorySpec, TaskCreate
from minion.events import EventBus, EventStore
from minion.repositories import SessionRepository, TaskRepository
from minion.runtime.context import ContextManager
from minion.runtime.intelligence import RepositoryIntelligence
from minion.runtime.router import GENERAL
from minion.runtime.workspace import Workspace


@pytest.mark.asyncio
async def test_context_compaction_updates_durable_summary(
    settings,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings.context_compact_every_events = 2
    database = Database(settings.database_url)
    await database.initialize()
    bus = EventBus(settings)
    workspace = Workspace(
        "env_context",
        tmp_path,
        {"demo": git_repo},
        {"demo": "main"},
    )
    intelligence = RepositoryIntelligence(settings, workspace)
    await intelligence.prepare()

    try:
        async with database.session_factory() as db:
            task = await TaskRepository(db).create(
                TaskCreate(
                    instruction="fix PaymentClient retry behavior",
                    repositories=[
                        RepositorySpec(
                            url="https://example.com/demo.git",
                            name="demo",
                        )
                    ],
                )
            )
            events = EventStore(db, bus)
            await events.append(
                task.id,
                task.session_id,
                EventType.AGENT_MESSAGE,
                {"content": "inspected PaymentClient"},
            )
            await events.append(
                task.id,
                task.session_id,
                EventType.TOOL_COMPLETED,
                {"tool": "read_file", "output": "important finding"},
            )

            context = ContextManager(
                settings,
                db,
                bus,
                intelligence,
                GENERAL,
            )
            await context.maybe_compact(task)

            session = await SessionRepository(db).get(task.session_id)
            assert session is not None
            assert "important finding" in session.summary
            assert session.last_compacted_sequence >= 2
    finally:
        await bus.close()
        await database.close()
