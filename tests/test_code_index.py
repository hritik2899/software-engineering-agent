"""Repository-intelligence tests.

These tests prove content-addressed reuse across Git commits, symbol discovery,
dependency resolution and reverse-impact analysis—the invariants that make
pre-indexing an acceleration rather than repeated full parsing.
"""
import asyncio
from pathlib import Path

from minion.runtime.code_index import RepositoryContextIndex


async def _run(cwd: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd)
    )
    assert await process.wait() == 0


async def _commit(repo: Path, message: str) -> None:
    await _run(repo, "git", "add", ".")
    await _run(repo, "git", "commit", "-m", message)


async def test_content_addressed_repository_index_reuses_unchanged_files(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _run(repo, "git", "init")
    await _run(
        repo, "git", "config", "user.email", "test@example.com"
    )
    await _run(
        repo, "git", "config", "user.name", "Test"
    )

    (repo / "helper.py").write_text(
        "def calculate_total(value: int) -> int:\n"
        "    return value + 1\n"
    )
    (repo / "app.py").write_text(
        "from helper import calculate_total\n\n"
        "def run():\n"
        "    return calculate_total(1)\n"
    )
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\n"
    )
    await _commit(repo, "initial")

    index = RepositoryContextIndex(tmp_path / "cache")
    first_build = await index.ensure_index(repo)
    first_overview = await index.overview(repo)

    assert first_build.parsed_files == 3
    assert first_build.reused_files == 0
    assert first_overview["file_count"] == 3
    assert first_overview["stats"]["symbol_count"] >= 2

    search = await index.search(repo, "calculate_total")
    assert any(
        item["path"] == "helper.py" for item in search
    )

    symbols = await index.symbol_context(
        repo, "calculate_total"
    )
    assert symbols[0]["path"] == "helper.py"

    impact = await index.impact_analysis(repo, "helper.py")
    assert "app.py" in impact["impacted_files"]

    (repo / "app.py").write_text(
        "from helper import calculate_total\n\n"
        "def run():\n"
        "    return calculate_total(2)\n"
    )
    await _commit(repo, "change app")

    second_build = await index.ensure_index(repo)
    assert second_build.parsed_files == 1
    assert second_build.reused_files == 2

    hints = await index.dependency_hints(repo, "app.py")
    assert "helper" in hints["imports_and_dependency_hints"]
    assert (
        "helper.py"
        in hints["resolved_repository_dependencies"]
    )
