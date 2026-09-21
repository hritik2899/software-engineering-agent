"""Runtime dependency construction."""
from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.events import EventBus, EventStore
from minion.runtime.agent import CodingAgent
from minion.runtime.context import ContextManager
from minion.runtime.llm import OpenAIClient
from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


def build_agent(
    settings: Settings, db: AsyncSession, bus: EventBus, workspace: Workspace
) -> CodingAgent:
    if settings.llm_provider != "openai":
        raise ValueError(f"unsupported LLM provider: {settings.llm_provider}")
    context = ContextManager(settings, db, bus)
    return CodingAgent(
        llm=OpenAIClient(settings),
        tools=ToolRegistry(workspace, settings.command_timeout_seconds),
        context=context,
        events=EventStore(db, bus),
        max_steps=settings.max_agent_steps,
    )
