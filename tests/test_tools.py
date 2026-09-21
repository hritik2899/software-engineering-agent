from pathlib import Path

import pytest

from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


@pytest.mark.asyncio
async def test_read_write_and_path_confinement(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = Workspace("env_test", tmp_path, {"repo": repo})
    tools = ToolRegistry(workspace)

    result = await tools.execute(
        "write_file", {"repo": "repo", "path": "src/example.txt", "content": "hello"}
    )
    assert result.ok

    result = await tools.execute(
        "read_file", {"repo": "repo", "path": "src/example.txt"}
    )
    assert result.ok
    assert "hello" in result.output

    escaped = await tools.execute(
        "read_file", {"repo": "repo", "path": "../outside.txt"}
    )
    assert not escaped.ok
    assert "escapes repository" in escaped.output


@pytest.mark.asyncio
async def test_shell_runs_in_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = Workspace("env_test", tmp_path, {"repo": repo})
    tools = ToolRegistry(workspace)
    result = await tools.execute("run_command", {"repo": "repo", "command": "pwd"})
    assert result.ok
    assert str(repo) in result.output
