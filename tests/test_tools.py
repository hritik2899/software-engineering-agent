"""Tool-boundary tests: confinement, patching and shell policy."""
import asyncio
from pathlib import Path

import pytest

from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


async def _git_repo(path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", "init", cwd=str(path)
    )
    assert await process.wait() == 0
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
    ):
        process = await asyncio.create_subprocess_exec(
            "git",
            "config",
            key,
            value,
            cwd=str(path),
        )
        assert await process.wait() == 0


@pytest.mark.asyncio
async def test_read_write_and_path_confinement(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = Workspace(
        "env_test", tmp_path, {"repo": repo}
    )
    tools = ToolRegistry(workspace)

    result = await tools.execute(
        "write_file",
        {
            "repo": "repo",
            "path": "src/example.txt",
            "content": "hello",
        },
    )
    assert result.ok

    result = await tools.execute(
        "read_file",
        {
            "repo": "repo",
            "path": "src/example.txt",
        },
    )
    assert result.ok
    assert "hello" in result.output

    escaped = await tools.execute(
        "read_file",
        {
            "repo": "repo",
            "path": "../outside.txt",
        },
    )
    assert not escaped.ok
    assert "escapes repository" in escaped.output


@pytest.mark.asyncio
async def test_shell_runs_in_repo_and_blocks_high_risk_commands(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = Workspace(
        "env_test", tmp_path, {"repo": repo}
    )
    tools = ToolRegistry(workspace)

    result = await tools.execute(
        "run_command",
        {"repo": "repo", "command": "pwd"},
    )
    assert result.ok
    assert str(repo) in result.output

    blocked = await tools.execute(
        "run_command",
        {
            "repo": "repo",
            "command": "sudo rm -rf /",
        },
    )
    assert not blocked.ok
    assert "blocked by policy" in blocked.output


@pytest.mark.asyncio
async def test_apply_patch_validates_and_edits_git_worktree(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _git_repo(repo)
    (repo / "hello.txt").write_text("old\n")

    workspace = Workspace(
        "env_test", tmp_path, {"repo": repo}
    )
    tools = ToolRegistry(workspace)
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    result = await tools.execute(
        "apply_patch",
        {"repo": "repo", "patch": patch},
    )
    assert result.ok
    assert (repo / "hello.txt").read_text() == "new\n"
