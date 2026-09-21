"""Central configuration for control-plane and execution-plane components."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MINION_", extra="ignore")

    env: str = "dev"
    database_url: str = "sqlite+aiosqlite:///./minion.db"
    redis_url: str | None = None
    workspace_root: Path = Path(".minion/workspaces")
    cache_root: Path = Path(".minion/cache")
    log_level: str = "INFO"
    api_key: str | None = None

    llm_provider: str = "openai"
    llm_model: str = "gpt-5"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    max_agent_steps: int = 30
    context_recent_events: int = 40
    context_max_chars: int = 60_000
    context_compact_every_events: int = 50

    environment_provider: str = "local"
    docker_image: str = "minion-sandbox:latest"
    command_timeout_seconds: int = 600
    warm_pool_size: int = 2
    retain_failed_workspaces: bool = True

    max_concurrent_tasks: int = 4
    task_lease_seconds: int = 45
    heartbeat_interval_seconds: int = 10
    recovery_scan_interval_seconds: int = 15

    default_repo_url: str = "https://github.com/octocat/Hello-World.git"
    default_base_branch: str = "main"

    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    github_api_url: str = "https://api.github.com"

    code_index_enabled: bool = True
    code_index_max_files: int = 3000
    code_index_max_file_bytes: int = 300_000

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
