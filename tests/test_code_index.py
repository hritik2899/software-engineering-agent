import asyncio
from pathlib import Path

from minion.runtime.code_index import RepositoryContextIndex


async def _run(cwd: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(*args, cwd=str(cwd))
    assert await process.wait() == 0


async def test_repository_context_cache(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _run(repo, "git", "init")
    await _run(repo, "git", "config", "user.email", "test@example.com")
    await _run(repo, "git", "config", "user.name", "Test")
    (repo / "app.py").write_text("import os\nfrom pathlib import Path\n")
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\n")
    await _run(repo, "git", "add", ".")
    await _run(repo, "git", "commit", "-m", "init")

    index = RepositoryContextIndex(tmp_path / "cache")
    first = await index.overview(repo)
    second = await index.overview(repo)

    assert first == second
    assert first["file_count"] == 2
    assert "pyproject.toml" in first["important_files"]

    hints = await index.dependency_hints(repo, "app.py")
    assert "import os" in hints["imports_and_dependency_hints"]
