from pathlib import Path

import pytest

from minion.runtime.intelligence import RepositoryIntelligence
from minion.runtime.workspace import Workspace


@pytest.mark.asyncio
async def test_repo_commit_partition_retrieval_and_dependency_neighbors(
    settings,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        "env_index",
        tmp_path,
        {"demo": git_repo},
        {"demo": "main"},
    )
    intelligence = RepositoryIntelligence(settings, workspace)
    await intelligence.prepare()

    partition = intelligence.partitions["demo"]
    assert partition.commit_sha
    assert any(item.path == "app.py" for item in partition.files)

    context = await intelligence.retrieve("PaymentClient retry")
    assert "app.py" in context
    assert "PaymentClient" in context

    neighbors = intelligence.dependency_neighbors("demo", "util.py")
    assert "app.py" in neighbors["possible_incoming_files"]
