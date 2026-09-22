"""Agent Skills tests.

These tests verify scope discovery, repository-skill precedence and automatic
activation from repository markers/task keywords without executing repository code.
"""
from pathlib import Path

from minion.runtime.skills import SkillManager
from minion.runtime.workspace import Workspace


def test_skill_discovery_auto_activation_and_repository_skill(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\n"
    )

    repo_skill = (
        repo
        / ".minion"
        / "skills"
        / "team-style"
    )
    repo_skill.mkdir(parents=True)
    (repo_skill / "SKILL.md").write_text(
        "---\n"
        "name: team-style\n"
        "description: Team-specific API conventions.\n"
        "keywords: [api]\n"
        "priority: 5\n"
        "---\n"
        "Always preserve backwards-compatible API "
        "response fields.\n"
    )

    workspace = Workspace(
        "env_test",
        tmp_path,
        {"demo": repo},
    )
    manager = SkillManager(
        workspace,
        user_root=tmp_path / "user-skills",
    )

    catalog = {
        item["name"]: item
        for item in manager.catalog()
    }
    assert "team-style" in catalog
    assert (
        catalog["team-style"]["source"]
        == "repository:demo"
    )

    active = manager.auto_activate(
        "Change the Python API and add tests"
    )
    assert "team-style" in active
    assert "python-testing" in active

    rendered = manager.render(active)
    assert (
        "backwards-compatible API response fields"
        in rendered
    )
