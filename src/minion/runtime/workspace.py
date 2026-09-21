"""Execution workspace abstraction with local and Docker providers.

Repository mirrors and shared dependency caches attack the most expensive coding-
agent cold-start costs: repeated clone, dependency download and environment setup.
"""
from __future__ import annotations

import asyncio
import hashlib
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
    container_name: str | None = None

    def repo_path(self, repo_name: str) -> Path:
        try:
            return self.repositories[repo_name]
        except KeyError as exc:
            raise EnvironmentError(f"unknown repository {repo_name!r}") from exc


def repo_name(spec: RepositorySpec) -> str:
    if spec.name:
        return spec.name
    tail = spec.url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "repo"


async def run_exec(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
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
    async def prepare(self) -> None: ...
    @abstractmethod
    async def allocate(self, task_id: str, repositories: list[RepositorySpec]) -> Workspace: ...
    @abstractmethod
    async def attach(self, environment_id: str, repositories: list[RepositorySpec]) -> Workspace | None: ...
    @abstractmethod
    async def release(self, workspace: Workspace) -> None: ...
    @abstractmethod
    async def healthy(self, workspace: Workspace) -> bool: ...


class LocalEnvironmentProvider(EnvironmentProvider):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo_cache = settings.cache_root / "repos"
        self.repo_cache.mkdir(parents=True, exist_ok=True)

    async def prepare(self) -> None:
        return None

    async def _populate(self, root: Path, task_id: str, repositories: list[RepositorySpec]) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for spec in repositories:
            name = repo_name(spec)
            mirror_key = hashlib.sha256(spec.url.encode()).hexdigest()
            mirror = self.repo_cache / f"{mirror_key}.git"
            if mirror.exists():
                await run_exec("git", "remote", "update", "--prune", cwd=mirror)
            else:
                await run_exec("git", "clone", "--mirror", spec.url, str(mirror))
            destination = root / name
            await run_exec(
                "git", "clone", "--reference-if-able", str(mirror),
                "--branch", spec.base_branch, spec.url, str(destination)
            )
            await run_exec("git", "checkout", "-b", f"agent/{task_id}", cwd=destination)
            result[name] = destination
        return result

    async def allocate(self, task_id: str, repositories: list[RepositorySpec]) -> Workspace:
        environment_id = new_id("env")
        root = self.settings.workspace_root / environment_id
        root.mkdir(parents=True, exist_ok=False)
        try:
            repos = await self._populate(root, task_id, repositories)
            return Workspace(environment_id, root, repos)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def attach(self, environment_id: str, repositories: list[RepositorySpec]) -> Workspace | None:
        root = self.settings.workspace_root / environment_id
        if not root.exists():
            return None
        repos = {repo_name(spec): root / repo_name(spec) for spec in repositories}
        workspace = Workspace(environment_id, root, repos)
        return workspace if await self.healthy(workspace) else None

    async def release(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)

    async def healthy(self, workspace: Workspace) -> bool:
        return workspace.root.exists() and all(path.exists() for path in workspace.repositories.values())


class DockerEnvironmentProvider(EnvironmentProvider):
    """Local Docker sandbox approximating a remote DevPod execution boundary.

    Source files remain on a host workspace bind-mounted at /workspace. Build
    commands execute inside the container. Dependency directories are shared
    caches, so repeat tasks avoid Maven/Gradle/npm/pip downloads.

    A real DevPod/Kubernetes adapter can keep this exact contract and replace only
    provisioning/health/release calls.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.local = LocalEnvironmentProvider(settings)
        dep = settings.cache_root / "dependencies"
        self.dep_cache = {
            "pip": dep / "pip",
            "npm": dep / "npm",
            "m2": dep / "m2",
            "gradle": dep / "gradle",
        }
        for path in self.dep_cache.values():
            path.mkdir(parents=True, exist_ok=True)

    async def prepare(self) -> None:
        # Pull once at startup: image warming removes repeated image cold start.
        await run_exec("docker", "pull", self.settings.docker_image, timeout=1200)

    async def allocate(self, task_id: str, repositories: list[RepositorySpec]) -> Workspace:
        workspace = await self.local.allocate(task_id, repositories)
        name = f"minion-{workspace.environment_id.replace('_', '-')[:40]}"
        args = [
            "docker", "run", "-d", "--name", name,
            "-v", f"{workspace.root.resolve()}:/workspace",
            "-v", f"{self.dep_cache['pip'].resolve()}:/root/.cache/pip",
            "-v", f"{self.dep_cache['npm'].resolve()}:/root/.npm",
            "-v", f"{self.dep_cache['m2'].resolve()}:/root/.m2",
            "-v", f"{self.dep_cache['gradle'].resolve()}:/root/.gradle",
            "-w", "/workspace",
            self.settings.docker_image,
            "sleep", "infinity",
        ]
        try:
            await run_exec(*args, timeout=300)
        except Exception:
            await self.local.release(workspace)
            raise
        workspace.container_name = name
        return workspace

    async def attach(self, environment_id: str, repositories: list[RepositorySpec]) -> Workspace | None:
        workspace = await self.local.attach(environment_id, repositories)
        if not workspace:
            return None
        name = f"minion-{environment_id.replace('_', '-')[:40]}"
        try:
            state = await run_exec("docker", "inspect", "-f", "{{.State.Running}}", name)
        except Exception:
            return None
        if state.strip() != "true":
            return None
        workspace.container_name = name
        return workspace

    async def release(self, workspace: Workspace) -> None:
        if workspace.container_name:
            try:
                await run_exec("docker", "rm", "-f", workspace.container_name)
            except Exception:
                pass
        await self.local.release(workspace)

    async def healthy(self, workspace: Workspace) -> bool:
        if not await self.local.healthy(workspace):
            return False
        if not workspace.container_name:
            return False
        try:
            state = await run_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", workspace.container_name
            )
            return state.strip() == "true"
        except Exception:
            return False


def build_environment_provider(settings: Settings) -> EnvironmentProvider:
    if settings.environment_provider == "local":
        return LocalEnvironmentProvider(settings)
    if settings.environment_provider == "docker":
        return DockerEnvironmentProvider(settings)
    raise ValueError(
        f"unsupported environment provider {settings.environment_provider!r}; "
        "implement EnvironmentProvider for DevPod/Kubernetes/cloud execution"
    )
