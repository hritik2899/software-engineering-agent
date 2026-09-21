"""Execution workspace abstraction and local implementation.

The workspace is disposable execution state. Durable task/session state lives in
SQL, and recoverable code state should be checkpointed to Git.  A future DevPod
or Kubernetes provider only needs to implement the same EnvironmentProvider
contract.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from minion.config import Settings
from minion.domain import RepositorySpec, new_id
from minion.errors import EnvironmentError


@dataclass(slots=True)
class Workspace:
    environment_id: str
    root: Path
    repositories: dict[str, Path] = field(default_factory=dict)

    def repo_path(self, repo_name: str) -> Path:
        try:
            return self.repositories[repo_name]
        except KeyError as exc:
            raise EnvironmentError(f"unknown repository {repo_name!r}") from exc


def _repo_name(spec: RepositorySpec) -> str:
    if spec.name:
        return spec.name
    tail = spec.url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "repo"


async def _run(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise EnvironmentError(f"command timed out: {' '.join(args)}")
    output = stdout.decode(errors="replace")
    if process.returncode != 0:
        raise EnvironmentError(f"command failed ({process.returncode}): {' '.join(args)}\n{output}")
    return output


class EnvironmentProvider(ABC):
    @abstractmethod
    async def allocate(
        self, task_id: str, repositories: list[RepositorySpec]
    ) -> Workspace: ...

    @abstractmethod
    async def release(self, workspace: Workspace) -> None: ...

    @abstractmethod
    async def healthy(self, workspace: Workspace) -> bool: ...


class LocalEnvironmentProvider(EnvironmentProvider):
    """Zero-setup provider with repository mirror caching.

    It is ideal for learning and CI. It is NOT a security sandbox, so untrusted
    tasks should use a Docker/DevPod/Kubernetes provider in production.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo_cache = settings.cache_root / "repos"
        self.repo_cache.mkdir(parents=True, exist_ok=True)

    async def allocate(
        self, task_id: str, repositories: list[RepositorySpec]
    ) -> Workspace:
        environment_id = new_id("env")
        root = self.settings.workspace_root / environment_id
        root.mkdir(parents=True, exist_ok=False)
        workspace = Workspace(environment_id=environment_id, root=root)

        try:
            for spec in repositories:
                name = _repo_name(spec)
                mirror_key = hashlib.sha256(spec.url.encode()).hexdigest()
                mirror = self.repo_cache / f"{mirror_key}.git"

                # Mirror cache removes repeated network clones. A fetch refreshes it
                # so base-branch content remains current.
                if mirror.exists():
                    await _run("git", "remote", "update", "--prune", cwd=mirror)
                else:
                    await _run("git", "clone", "--mirror", spec.url, str(mirror))

                destination = root / name
                await _run(
                    "git",
                    "clone",
                    "--reference-if-able",
                    str(mirror),
                    "--branch",
                    spec.base_branch,
                    spec.url,
                    str(destination),
                )
                branch = f"agent/{task_id}"
                await _run("git", "checkout", "-b", branch, cwd=destination)
                workspace.repositories[name] = destination
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

        return workspace

    async def release(self, workspace: Workspace) -> None:
        if workspace.root.exists():
            shutil.rmtree(workspace.root, ignore_errors=True)

    async def healthy(self, workspace: Workspace) -> bool:
        return workspace.root.exists() and all(path.exists() for path in workspace.repositories.values())
