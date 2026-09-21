from pathlib import Path

import pytest

from minion.db import Database
from minion.domain import EventType, RepositorySpec, TaskCreate
from minion.events import EventBus, EventStore
from minion.repositories import CheckpointRepository, TaskRepository
from minion.runtime.factory import build_agent
from minion.runtime.workspace import Workspace


@pytest.mark.asyncio
async def test_mock_agent_edits_verifies_inspects_and_checkpoints(
    settings,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    database = Database(settings.database_url)
    await database.initialize()
    bus = EventBus(settings)
    workspace = Workspace(
        "env_agent",
        tmp_path,
        {"demo": git_repo},
        {"demo": "main"},
    )

    async def false() -> bool:
        return False

    try:
        async with database.session_factory() as db:
            task = await TaskRepository(db).create(
                TaskCreate(
                    instruction="add a demonstration artifact",
                    repositories=[
                        RepositorySpec(
                            url="https://example.com/demo.git",
                            name="demo",
                        )
                    ],
                )
            )
            agent = await build_agent(
                settings,
                db,
                bus,
                workspace,
                should_cancel=false,
                should_pause=false,
            )
            result = await agent.run(task)

            assert result["steps"] == 6
            assert (git_repo / "MINION_AGENT_DEMO.txt").exists()

            events = await EventStore(db, bus).list_after(task.id)
            tool_names = [
                event.payload.get("tool")
                for event in events
                if event.type == EventType.TOOL_COMPLETED
            ]
            assert {"write_file", "run_command", "git_diff", "checkpoint"} <= set(
                tool_names
            )

            checkpoints = await CheckpointRepository(db).latest_for_task(task.id)
            assert "demo" in checkpoints
            assert "MINION_AGENT_DEMO.txt" in checkpoints["demo"].binary_patch
    finally:
        await bus.close()
        await database.close()
