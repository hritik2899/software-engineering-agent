"""Runtime dependency construction."""
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.events import EventBus, EventStore
from minion.runtime.agent import CodingAgent
from minion.runtime.context import ContextManager
from minion.runtime.intelligence import RepositoryIntelligence
from minion.runtime.llm import MockLLMClient, OpenAIClient
from minion.runtime.router import GENERAL
from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


async def build_agent(
    settings: Settings,
    db: AsyncSession,
    bus: EventBus,
    workspace: Workspace,
    should_cancel: Callable[[], Awaitable[bool]],
    should_pause: Callable[[], Awaitable[bool]],
) -> CodingAgent:
    intelligence = RepositoryIntelligence(settings, workspace)
    await intelligence.prepare()
    context = ContextManager(settings, db, bus, intelligence, GENERAL)
    tools = ToolRegistry(
        workspace,
        intelligence,
        settings.command_timeout_seconds,
    )

    if settings.llm_provider == "openai":
        llm = OpenAIClient(settings)
    elif settings.llm_provider == "mock":
        llm = MockLLMClient(next(iter(workspace.repositories)))
    else:
        raise ValueError(f"unsupported LLM provider: {settings.llm_provider}")

    return CodingAgent(
        llm=llm,
        tools=tools,
        context=context,
        events=EventStore(db, bus),
        max_steps=settings.max_agent_steps,
        should_cancel=should_cancel,
        should_pause=should_pause,
    )
