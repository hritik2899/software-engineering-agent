"""Domain-contract tests.

These tests protect stable task/session/repository serialization and enum semantics so
API, persistence and runtime layers keep speaking the same vocabulary.
"""
from minion.domain import RepositorySpec, TaskCreate


def test_task_can_use_default_repo_at_api_layer():
    task = TaskCreate(instruction="fix the failing test")
    assert task.repositories == []


def test_multi_repo_task():
    task = TaskCreate(
        instruction="update schema and consumer",
        repositories=[
            RepositorySpec(url="https://github.com/acme/schema.git", name="schema"),
            RepositorySpec(url="https://github.com/acme/service.git", name="service"),
        ],
    )
    assert [repo.name for repo in task.repositories] == ["schema", "service"]
