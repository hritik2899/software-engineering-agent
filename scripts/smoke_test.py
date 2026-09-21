"""Deterministic end-to-end smoke test for the complete Minion control plane."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from minion.config import Settings
from minion.db import Database
from minion.domain import RepositorySpec, TaskCreate, TaskStatus
from minion.events import EventBus, EventStore
from minion.orchestrator import Orchestrator
from minion.queue import build_queue
from minion.repositories import TaskRepository
from minion.runtime.workspace import build_environment_provider


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="minion-smoke-") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        run_git(source, "init", "-b", "main")
        run_git(source, "config", "user.name", "Smoke Test")
        run_git(source, "config", "user.email", "smoke@example.com")
        (source / "README.md").write_text("# smoke repository\n")
        run_git(source, "add", ".")
        run_git(source, "commit", "-m", "initial")

        settings = Settings(
            _env_file=None,
            env="test",
            database_url=f"sqlite+aiosqlite:///{root / 'smoke.db'}",
            redis_url=None,
            workspace_root=root / "workspaces",
            cache_root=root / "cache",
            llm_provider="mock",
            environment_provider="local",
            max_agent_steps=12,
            heartbeat_interval_seconds=1,
            task_lease_seconds=10,
            recovery_scan_interval_seconds=1,
        )
        settings.ensure_directories()

        database = Database(settings.database_url)
        await database.initialize()
        bus = EventBus(settings)
        orchestrator = Orchestrator(
            settings,
            database.session_factory,
            build_queue(settings),
            build_environment_provider(settings),
            bus,
        )
        await orchestrator.start()

        try:
            submitted = await orchestrator.submit(
                TaskCreate(
                    instruction="create and verify the smoke-test artifact",
                    repositories=[
                        RepositorySpec(
                            url=source.as_uri(),
                            base_branch="main",
                            name="demo",
                        )
                    ],
                )
            )

            final = submitted
            for _ in range(200):
                await asyncio.sleep(0.1)
                async with database.session_factory() as db:
                    final = await TaskRepository(db).require(submitted.id)
                if final.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    break

            if final.status != TaskStatus.COMPLETED:
                raise RuntimeError(
                    f"smoke task did not complete: {final.status}: {final.error}"
                )

            async with database.session_factory() as db:
                events = await EventStore(db, bus).list_after(final.id)
            print(
                f"PASS task={final.id} status={final.status} "
                f"events={len(events)} steps={final.result.get('steps') if final.result else None}"
            )
        finally:
            await orchestrator.stop()
            await bus.close()
            await database.close()


if __name__ == "__main__":
    asyncio.run(main())
