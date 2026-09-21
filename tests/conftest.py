from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minion.config import Settings


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'minion-test.db'}",
        redis_url=None,
        workspace_root=tmp_path / "workspaces",
        cache_root=tmp_path / "cache",
        llm_provider="mock",
        environment_provider="local",
        max_agent_steps=12,
        context_compact_every_events=4,
        task_lease_seconds=10,
        heartbeat_interval_seconds=1,
        recovery_scan_interval_seconds=1,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "util.py").write_text(
        "def retry(value: int = 1) -> int:\n"
        "    return value\n"
    )
    (repo / "app.py").write_text(
        "from util import retry\n\n"
        "class PaymentClient:\n"
        "    def send(self) -> int:\n"
        "        return retry(1)\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", repo.as_uri())
    git(repo, "fetch", "origin", "main")
    return repo
