"""Runtime dependency construction for one allocated workspace.

The orchestrator owns task/environment lifecycle; this factory wires the components
specific to one running coding agent.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from minion.config import Settings
from minion.events import EventBus, EventStore
from minion.runtime.agent import CodingAgent
from minion.runtime.context import ContextManager
from minion.runtime.llm import OpenAIClient
from minion.runtime.mcp_tools import MCPManager
from minion.runtime.skills import SkillManager
from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


def build_agent(
    settings: Settings,
    db: AsyncSession,
    bus: EventBus,
    workspace: Workspace,
) -> CodingAgent:
    if settings.llm_provider != "openai":
        raise ValueError(
            f"unsupported LLM provider: {settings.llm_provider}"
        )

    skills = SkillManager(
        workspace,
        user_root=settings.skills_user_root,
        max_active=settings.max_active_skills,
    )
    context = ContextManager(
        settings,
        db,
        bus,
        skills=skills,
    )
    mcp = MCPManager(settings.mcp_servers_path)
    tools = ToolRegistry(
        workspace,
        settings.command_timeout_seconds,
        cache_root=settings.cache_root,
        command_policy_mode=(
            settings.command_policy_mode
        ),
        mcp_manager=mcp,
    )
    return CodingAgent(
        llm=OpenAIClient(settings),
        tools=tools,
        context=context,
        events=EventStore(db, bus),
        max_steps=settings.max_agent_steps,
    )
