"""Central configuration.

All machine-, repo- and credential-specific values live outside the codebase.  This
keeps orchestration code deterministic and makes the project safe to run against a
throwaway repository before pointing it at a real one.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MINION_",
        extra="ignore",
    )

    env: str = "dev"
    database_url: str = "sqlite+aiosqlite:///./minion.db"
    redis_url: str | None = None
    workspace_root: Path = Path(".minion/workspaces")
    cache_root: Path = Path(".minion/cache")
    log_level: str = "INFO"

    llm_provider: str = "openai"
    llm_model: str = "gpt-5"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    max_agent_steps: int = 30
    context_recent_events: int = 40
    context_max_chars: int = 60_000

    environment_provider: str = "local"
    docker_image: str = "python:3.12-bookworm"
    command_timeout_seconds: int = 600
    warm_pool_size: int = 2

    default_repo_url: str = "https://github.com/octocat/Hello-World.git"
    default_base_branch: str = "main"

    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    github_api_url: str = "https://api.github.com"

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
