import pytest

from minion.db import Database
from minion.domain import EventType, RepositorySpec, TaskCreate
from minion.events import EventBus, EventStore
from minion.repositories import SessionRepository, TaskLeaseRepository, TaskRepository


@pytest.mark.asyncio
async def test_task_lease_prevents_duplicate_execution(settings) -> None:
    database = Database(settings.database_url)
    await database.initialize()
    try:
        async with database.session_factory() as db:
            task = await TaskRepository(db).create(
                TaskCreate(
                    instruction="fix retry handling",
                    repositories=[
                        RepositorySpec(
                            url="https://example.com/demo.git",
                            name="demo",
                        )
                    ],
                )
            )
            leases = TaskLeaseRepository(db)
            assert await leases.acquire(task.id, "worker-a", 30)
            assert not await leases.acquire(task.id, "worker-b", 30)
            assert await leases.owned_by(task.id, "worker-a")
            await leases.release(task.id, "worker-a")
            assert await leases.acquire(task.id, "worker-b", 30)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_events_are_ordered_replayable_and_context_memory_persists(settings) -> None:
    database = Database(settings.database_url)
    await database.initialize()
    bus = EventBus(settings)
    try:
        async with database.session_factory() as db:
            task = await TaskRepository(db).create(
                TaskCreate(
                    instruction="fix retry handling",
                    repositories=[
                        RepositorySpec(
                            url="https://example.com/demo.git",
                            name="demo",
                        )
                    ],
                )
            )
            store = EventStore(db, bus)
            first = await store.append(
                task.id,
                task.session_id,
                EventType.AGENT_MESSAGE,
                {"content": "one"},
            )
            second = await store.append(
                task.id,
                task.session_id,
                EventType.USER_MESSAGE,
                {"message": "do not touch legacy code"},
            )

            replay = await store.list_after(task.id, first.sequence)
            assert [event.sequence for event in replay] == [second.sequence]

            sessions = SessionRepository(db)
            await sessions.append_instruction(
                task.session_id,
                "do not touch legacy code",
            )
            await sessions.update_memory(
                task.session_id,
                current_plan=["inspect", "fix", "test"],
            )
            session = await sessions.get(task.session_id)
            assert session is not None
            assert session.current_plan[-1] == "test"
            assert "do not touch legacy code" in session.active_constraints
    finally:
        await bus.close()
        await database.close()
