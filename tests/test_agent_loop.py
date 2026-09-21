from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from minion.config import Settings
from minion.domain import RepositorySpec, TaskStatus, TaskView, utcnow
from minion.events import EventBus, EventStore
from minion.models import Base, SessionRow, TaskRow
from minion.runtime.agent import CodingAgent
from minion.runtime.context import ContextManager
from minion.runtime.llm import ModelTurn, ToolCall
from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


class ScriptedLLM:
    def __init__(self):
        self.turn = 0

    async def complete(self, messages, tools):
        del messages, tools
        self.turn += 1
        if self.turn == 1:
            return ModelTurn(
                "",
                [ToolCall("1", "set_plan", {"steps": ["write file", "verify"]})],
            )
        if self.turn == 2:
            return ModelTurn(
                "",
                [
                    ToolCall(
                        "2",
                        "write_file",
                        {"repo": "demo", "path": "answer.txt", "content": "done\n"},
                    )
                ],
            )
        if self.turn == 3:
            return ModelTurn(
                "",
                [
                    ToolCall(
                        "3",
                        "run_command",
                        {"repo": "demo", "command": "test -f answer.txt"},
                    )
                ],
            )
        return ModelTurn(
            "",
            [
                ToolCall(
                    "4",
                    "finish_task",
                    {
                        "summary": "created answer.txt",
                        "verification": ["test -f answer.txt passed"],
                    },
                )
            ],
        )


async def test_agent_reason_tool_memory_finish_loop(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    repo = tmp_path / "demo"
    repo.mkdir()
    workspace = Workspace("env_test", tmp_path, {"demo": repo})
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        cache_root=tmp_path / "cache",
        context_compaction_threshold=1000,
    )

    now = utcnow()
    task = TaskView(
        id="task_test",
        session_id="session_test",
        environment_id="env_test",
        user_id="tester",
        instruction="create answer.txt",
        status=TaskStatus.RUNNING,
        repositories=[
            RepositorySpec(url="https://github.com/example/demo.git", name="demo")
        ],
        publish_pr=False,
        created_at=now,
        updated_at=now,
    )

    async with sessions() as db:
        db.add(
            TaskRow(
                id=task.id,
                session_id=task.session_id,
                environment_id=task.environment_id,
                user_id=task.user_id,
                instruction=task.instruction,
                status=task.status.value,
                repositories=[r.model_dump() for r in task.repositories],
                publish_pr=False,
            )
        )
        db.add(SessionRow(id=task.session_id, task_id=task.id))
        await db.commit()

        bus = EventBus()
        context = ContextManager(settings, db, bus)
        events = EventStore(db, bus)
        agent = CodingAgent(
            llm=ScriptedLLM(),
            tools=ToolRegistry(workspace, cache_root=settings.cache_root),
            context=context,
            events=events,
            max_steps=10,
        )

        result = await agent.run(task)
        assert result["summary"] == "created answer.txt"
        assert (repo / "answer.txt").read_text() == "done\n"

        session = await context.sessions.get(task.session_id)
        assert session is not None
        assert session.current_plan == ["write file", "verify"]

    await engine.dispose()
