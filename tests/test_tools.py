from pathlib import Path

import pytest

from minion.runtime.intelligence import RepositoryIntelligence
from minion.runtime.tools import ToolRegistry
from minion.runtime.workspace import Workspace


@pytest.mark.asyncio
async def test_read_write_shell_and_path_confinement(
    settings,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        "env_test",
        tmp_path,
        {"demo": git_repo},
        {"demo": "main"},
    )
    intelligence = RepositoryIntelligence(settings, workspace)
    await intelligence.prepare()
    tools = ToolRegistry(workspace, intelligence)

    written = await tools.execute(
        "write_file",
        {
            "repo": "demo",
            "path": "src/example.txt",
            "content": "hello",
        },
    )
    assert written.ok

    read = await tools.execute(
        "read_file",
        {"repo": "demo", "path": "src/example.txt"},
    )
    assert read.ok
    assert "hello" in read.output

    escaped = await tools.execute(
        "read_file",
        {"repo": "demo", "path": "../outside.txt"},
    )
    assert not escaped.ok
    assert "escapes repository" in escaped.output

    shell = await tools.execute(
        "run_command",
        {"repo": "demo", "command": "pwd"},
    )
    assert shell.ok
    assert str(git_repo) in shell.output
