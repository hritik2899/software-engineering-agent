"""End-to-end orchestration test.

The fixture uses file-backed SQLite so worker tasks and the test client can open
independent async connections without losing schema visibility. Fake environment and
agent implementations isolate orchestration behavior from Git and LLM dependencies.
"""
import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from minion.config import Settings
from minion.domain import RepositorySpec, TaskCreate, TaskStatus
from minion.events import EventBus
from minion.models import Base
from minion.orchestrator import Orchestrator
from minion.queue import InMemoryWorkQueue
from minion.repositories import TaskRepository
from minion.runtime.workspace import EnvironmentProvider, Workspace


class FakeEnvironmentProvider(EnvironmentProvider):
    def __init__(self, root: Path):
        self.root = root
        self.released: list[bool] = []

    async def prepare(self) -> None:
        return None

    async def allocate(self, task_id, repositories):
        del repositories
        root = self.root / f"env_{task_id}"
        root.mkdir(parents=True, exist_ok=True)
        repo = root / "demo"
        repo.mkdir()
        return Workspace("env_fake", root, {"demo": repo})

    async def attach(self, environment_id, repositories):
        del environment_id, repositories
        return None

    async def release(self, workspace, *, destroy_workspace=True):
        del workspace
        self.released.append(destroy_workspace)

    async def healthy(self, workspace):
        return workspace.root.exists()

    async def shutdown(self) -> None:
        return None


class FakeAgent:
    async def run(self, task):
        return {
            "summary": f"completed {task.id}",
            "verification": ["fake integration verification"],
        }


async def test_orchestrator_end_to_end(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'orchestrator.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        cache_root=tmp_path / "cache",
        worker_concurrency=1,
        heartbeat_interval_seconds=60,
    )
    queue = InMemoryWorkQueue()
    environment = FakeEnvironmentProvider(tmp_path / "envs")
    bus = EventBus()

    def agent_factory(settings, db, bus, workspace):
        del settings, db, bus, workspace
        return FakeAgent()

    orchestrator = Orchestrator(
        settings,
        queue,
        environment,
        bus,
        session_factory=session_factory,
        agent_factory=agent_factory,
    )
    await orchestrator.start()
    try:
        task = await orchestrator.submit(
            TaskCreate(
                instruction="make a verified test change",
                repositories=[
                    RepositorySpec(
                        url="https://github.com/example/demo.git",
                        name="demo",
                    )
                ],
            )
        )

        for _ in range(100):
            async with session_factory() as db:
                current = await TaskRepository(db).require(task.id)
            if current.status == TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.01)

        assert current.status == TaskStatus.COMPLETED
        assert current.environment_id == "env_fake"
        assert current.result is not None
        assert "completed" in current.result["summary"]
        assert environment.released == [True]
    finally:
        await orchestrator.stop()
        await engine.dispose()
