"""Execution workspace providers and repository cold-start caches."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from minion.config import Settings
from minion.domain import RepositorySpec, new_id
from minion.errors import EnvironmentError
from minion.logging import logger

log = logger(__name__)


@dataclass(slots=True)
class Workspace:
    environment_id: str
    root: Path
    repositories: dict[str, Path] = field(default_factory=dict)
    base_branches: dict[str, str] = field(default_factory=dict)
    container_name: str | None = None
    container_workspace_root: str = "/workspace"

    def repo_path(self, name: str) -> Path:
        try:
            return self.repositories[name]
        except KeyError as exc:
            raise EnvironmentError(f"unknown repository {name!r}") from exc


def repo_name(spec: RepositorySpec) -> str:
    if spec.name:
        return spec.name
    tail = spec.url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "repo"


def git_auth_environment(settings: Settings) -> dict[str, str]:
    environment = os.environ.copy()
    if settings.github_token:
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: bearer {settings.github_token}",
            }
        )
    return environment


async def run_exec(
    *args: str,
    cwd: Path | None = None,
    timeout_seconds: int = 600,
    env: dict[str, str] | None = None,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise EnvironmentError(f"command timed out: {' '.join(args)}") from exc

    output = stdout.decode(errors="replace")
    if process.returncode != 0:
        raise EnvironmentError(
            f"command failed ({process.returncode}): {' '.join(args)}\n{output}"
        )
    return output


class EnvironmentProvider(ABC):
    @abstractmethod
    async def prepare(self) -> None: ...

    @abstractmethod
    async def allocate(
        self,
        task_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace: ...

    @abstractmethod
    async def attach(
        self,
        environment_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace | None: ...

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

    async def _populate(
        self,
        root: Path,
        task_id: str,
        repositories: list[RepositorySpec],
    ) -> tuple[dict[str, Path], dict[str, str]]:
        result: dict[str, Path] = {}
        base_branches: dict[str, str] = {}
        auth_env = git_auth_environment(self.settings)

        for spec in repositories:
            name = repo_name(spec)
            mirror_key = hashlib.sha256(spec.url.encode()).hexdigest()
            mirror = self.repo_cache / f"{mirror_key}.git"
            if mirror.exists():
                await run_exec("git", "remote", "update", "--prune", cwd=mirror, env=auth_env)
            else:
                await run_exec("git", "clone", "--mirror", spec.url, str(mirror), env=auth_env)

            destination = root / name
            await run_exec(
                "git",
                "clone",
                "--reference-if-able",
                str(mirror),
                "--branch",
                spec.base_branch,
                spec.url,
                str(destination),
                env=auth_env,
            )
            await run_exec("git", "config", "user.name", "Minion Agent", cwd=destination)
            await run_exec(
                "git",
                "config",
                "user.email",
                "minion-agent@localhost",
                cwd=destination,
            )
            await run_exec(
                "git",
                "checkout",
                "-b",
                f"agent/{task_id}",
                cwd=destination,
                env=auth_env,
            )
            result[name] = destination
            base_branches[name] = spec.base_branch

        return result, base_branches

    async def allocate(
        self,
        task_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace:
        environment_id = new_id("env")
        root = self.settings.workspace_root / environment_id
        root.mkdir(parents=True, exist_ok=False)
        try:
            repos, branches = await self._populate(root, task_id, repositories)
            return Workspace(environment_id, root, repos, branches)
        except (OSError, EnvironmentError):
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def attach(
        self,
        environment_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace | None:
        root = self.settings.workspace_root / environment_id
        if not root.exists():
            return None
        repos = {repo_name(spec): root / repo_name(spec) for spec in repositories}
        branches = {repo_name(spec): spec.base_branch for spec in repositories}
        workspace = Workspace(environment_id, root, repos, branches)
        return workspace if await self.healthy(workspace) else None

    async def release(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)

    async def healthy(self, workspace: Workspace) -> bool:
        return workspace.root.exists() and all(
            path.exists() for path in workspace.repositories.values()
        )


class DockerEnvironmentProvider(EnvironmentProvider):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.local = LocalEnvironmentProvider(settings)
        self.dep_cache = self._dependency_caches(settings)

    @staticmethod
    def _dependency_caches(settings: Settings) -> dict[str, Path]:
        root = settings.cache_root / "dependencies"
        caches = {
            "pip": root / "pip",
            "npm": root / "npm",
            "m2": root / "m2",
            "gradle": root / "gradle",
        }
        for path in caches.values():
            path.mkdir(parents=True, exist_ok=True)
        return caches

    async def prepare(self) -> None:
        await run_exec("docker", "image", "inspect", self.settings.docker_image, timeout_seconds=60)

    def _docker_cache_mounts(self) -> list[str]:
        return [
            "-v",
            f"{self.dep_cache['pip'].resolve()}:/root/.cache/pip",
            "-v",
            f"{self.dep_cache['npm'].resolve()}:/root/.npm",
            "-v",
            f"{self.dep_cache['m2'].resolve()}:/root/.m2",
            "-v",
            f"{self.dep_cache['gradle'].resolve()}:/root/.gradle",
        ]

    async def allocate(
        self,
        task_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace:
        workspace = await self.local.allocate(task_id, repositories)
        name = f"minion-{workspace.environment_id.replace('_', '-')[:40]}"
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--cpus",
            "4",
            "--memory",
            "8g",
            "--pids-limit",
            "2048",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{workspace.root.resolve()}:/workspace",
            *self._docker_cache_mounts(),
            "-w",
            "/workspace",
            self.settings.docker_image,
            "sleep",
            "infinity",
        ]
        try:
            await run_exec(*args, timeout_seconds=300)
        except (OSError, EnvironmentError):
            await self.local.release(workspace)
            raise
        workspace.container_name = name
        return workspace

    async def attach(
        self,
        environment_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace | None:
        workspace = await self.local.attach(environment_id, repositories)
        if workspace is None:
            return None
        name = f"minion-{environment_id.replace('_', '-')[:40]}"
        try:
            state = await run_exec("docker", "inspect", "-f", "{{.State.Running}}", name)
        except (OSError, EnvironmentError):
            return None
        if state.strip() != "true":
            return None
        workspace.container_name = name
        return workspace

    async def release(self, workspace: Workspace) -> None:
        if workspace.container_name:
            try:
                await run_exec("docker", "rm", "-f", workspace.container_name)
            except (OSError, EnvironmentError):
                log.exception(
                    "docker_environment_cleanup_failed",
                    environment_id=workspace.environment_id,
                )
        await self.local.release(workspace)

    async def healthy(self, workspace: Workspace) -> bool:
        if not await self.local.healthy(workspace) or not workspace.container_name:
            return False
        try:
            state = await run_exec(
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                workspace.container_name,
            )
            return state.strip() == "true"
        except (OSError, EnvironmentError):
            return False


class PooledDockerEnvironmentProvider(DockerEnvironmentProvider):
    """Single-tenant warm container pool used to study cold-start elimination."""

    PREFIX = "minion-warm-"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._warm: asyncio.Queue[str] = asyncio.Queue()

    async def prepare(self) -> None:
        await super().prepare()
        listing = await run_exec(
            "docker",
            "ps",
            "--filter",
            "status=running",
            "--format",
            "{{.Names}}",
        )
        for name in listing.splitlines():
            if name.startswith(self.PREFIX):
                self._warm.put_nowait(name)
        await self._replenish()

    async def _new_warm_container(self) -> str:
        name = f"{self.PREFIX}{uuid4().hex[:12]}"
        await run_exec(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--cpus",
            "4",
            "--memory",
            "8g",
            "--pids-limit",
            "2048",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{self.settings.workspace_root.resolve()}:/workspace-root",
            *self._docker_cache_mounts(),
            self.settings.docker_image,
            "sleep",
            "infinity",
        )
        return name

    async def _replenish(self) -> None:
        while self._warm.qsize() < self.settings.warm_pool_size:
            self._warm.put_nowait(await self._new_warm_container())

    async def allocate(
        self,
        task_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace:
        workspace = await self.local.allocate(task_id, repositories)
        try:
            try:
                warm_name = self._warm.get_nowait()
            except asyncio.QueueEmpty:
                warm_name = await self._new_warm_container()
            assigned = f"minion-{workspace.environment_id.replace('_', '-')[:40]}"
            await run_exec("docker", "rename", warm_name, assigned)
            workspace.container_name = assigned
            workspace.container_workspace_root = f"/workspace-root/{workspace.environment_id}"
            asyncio.create_task(self._replenish())
            return workspace
        except (OSError, EnvironmentError):
            await self.local.release(workspace)
            raise

    async def attach(
        self,
        environment_id: str,
        repositories: list[RepositorySpec],
    ) -> Workspace | None:
        workspace = await self.local.attach(environment_id, repositories)
        if workspace is None:
            return None
        name = f"minion-{environment_id.replace('_', '-')[:40]}"
        try:
            state = await run_exec("docker", "inspect", "-f", "{{.State.Running}}", name)
        except (OSError, EnvironmentError):
            return None
        if state.strip() != "true":
            return None
        workspace.container_name = name
        workspace.container_workspace_root = f"/workspace-root/{environment_id}"
        return workspace

    async def release(self, workspace: Workspace) -> None:
        await super().release(workspace)
        await self._replenish()


async def restore_binary_patch(workspace: Workspace, repo: str, patch: str) -> None:
    if not patch.strip():
        return
    repository = workspace.repo_path(repo)
    patch_path = workspace.root / f".minion-recovery-{repo}.patch"
    patch_path.write_text(patch)
    try:
        await run_exec(
            "git",
            "apply",
            "--index",
            "--3way",
            str(patch_path),
            cwd=repository,
        )
        await run_exec(
            "git",
            "commit",
            "-m",
            "restore durable Minion checkpoint",
            "--allow-empty",
            cwd=repository,
        )
    finally:
        patch_path.unlink(missing_ok=True)


def build_environment_provider(settings: Settings) -> EnvironmentProvider:
    if settings.environment_provider == "local":
        return LocalEnvironmentProvider(settings)
    if settings.environment_provider == "docker":
        return DockerEnvironmentProvider(settings)
    if settings.environment_provider == "docker_pool":
        return PooledDockerEnvironmentProvider(settings)
    raise ValueError(
        f"unsupported environment provider {settings.environment_provider!r}; "
        "implement EnvironmentProvider for DevPod/Kubernetes/cloud execution"
    )
