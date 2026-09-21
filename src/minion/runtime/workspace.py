"""Execution environment providers.

Docker mode implements a warm execution pool: containers are prestarted once and
reused across tasks while each task keeps a separate host workspace. Repository
mirrors and dependency caches remove repeated clone/download cold starts.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from minion.config import Settings
from minion.domain import RepositorySpec, new_id
from minion.errors import EnvironmentError
from minion.git_auth import git_environment
from minion.logging import logger

log = logger(__name__)


@dataclass(slots=True)
class Workspace:
    environment_id: str
    root: Path
    repositories: dict[str, Path] = field(default_factory=dict)
    container_name: str | None = None
    container_workspace_root: str | None = None

    def repo_path(self, repo_name: str) -> Path:
        try:
            return self.repositories[repo_name]
        except KeyError as exc:
            raise EnvironmentError(f"unknown repository {repo_name!r}") from exc

    def container_repo_path(self, repo_name: str) -> str:
        if not self.container_workspace_root:
            raise EnvironmentError("workspace has no container path")
        relative = self.repo_path(repo_name).relative_to(self.root)
        return f"{self.container_workspace_root.rstrip('/')}/{relative}"


def repo_name(spec: RepositorySpec) -> str:
    if spec.name:
        return spec.name
    tail = spec.url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "repo"


async def run_exec(
    *args: str,
    cwd: Path | None = None,
    timeout: int = 600,
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
        self, task_id: str, repositories: list[RepositorySpec]
    ) -> Workspace: ...

    @abstractmethod
    async def attach(
        self, environment_id: str, repositories: list[RepositorySpec]
    ) -> Workspace | None: ...

    @abstractmethod
    async def release(self, workspace: Workspace, *, destroy_workspace: bool = True) -> None: ...

    @abstractmethod
    async def healthy(self, workspace: Workspace) -> bool: ...

    @abstractmethod
    async def shutdown(self) -> None: ...


class LocalEnvironmentProvider(EnvironmentProvider):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo_cache = settings.cache_root / "repos"
        self.repo_cache.mkdir(parents=True, exist_ok=True)

    async def prepare(self) -> None:
        return None

    async def _populate(
        self, root: Path, task_id: str, repositories: list[RepositorySpec]
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        auth_env = git_environment(self.settings)
        for spec in repositories:
            name = repo_name(spec)
            mirror_key = hashlib.sha256(spec.url.encode()).hexdigest()
            mirror = self.repo_cache / f"{mirror_key}.git"
            if mirror.exists():
                await run_exec(
                    "git", "remote", "update", "--prune", cwd=mirror, env=auth_env
                )
            else:
                await run_exec(
                    "git", "clone", "--mirror", spec.url, str(mirror), env=auth_env
                )
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
            await run_exec(
                "git", "checkout", "-b", f"agent/{task_id}", cwd=destination
            )
            result[name] = destination
        return result

    async def allocate(
        self, task_id: str, repositories: list[RepositorySpec]
    ) -> Workspace:
        environment_id = new_id("env")
        root = self.settings.workspace_root / environment_id
        root.mkdir(parents=True, exist_ok=False)
        try:
            repos = await self._populate(root, task_id, repositories)
            return Workspace(environment_id, root, repos)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def attach(
        self, environment_id: str, repositories: list[RepositorySpec]
    ) -> Workspace | None:
        root = self.settings.workspace_root / environment_id
        if not root.exists():
            return None
        repos = {repo_name(spec): root / repo_name(spec) for spec in repositories}
        workspace = Workspace(environment_id, root, repos)
        return workspace if await self.healthy(workspace) else None

    async def release(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)

    async def healthy(self, workspace: Workspace) -> bool:
        return workspace.root.exists() and all(
            path.exists() for path in workspace.repositories.values()
        )

    async def shutdown(self) -> None:
        return None


class DockerEnvironmentProvider(EnvironmentProvider):
    """Reusable local Docker sandboxes approximating remote DevPods."""

    POOL_LABEL = "minion.warm_pool=true"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.local = LocalEnvironmentProvider(settings)
        dep = settings.cache_root / "dependencies"
        self.dep_cache = {
            "pip": dep / "pip",
            "npm": dep / "npm",
            "m2": dep / "m2",
            "gradle": dep / "gradle",
            "go": dep / "go",
        }
        for path in self.dep_cache.values():
            path.mkdir(parents=True, exist_ok=True)
        self._pool: asyncio.Queue[str] = asyncio.Queue()
        self._containers: set[str] = set()

    async def _ensure_image(self) -> None:
        try:
            await run_exec("docker", "image", "inspect", self.settings.docker_image)
            return
        except EnvironmentError:
            if not self.settings.docker_pull_image:
                raise EnvironmentError(
                    f"Docker image {self.settings.docker_image!r} does not exist. "
                    "Build it with make sandbox or enable MINION_DOCKER_PULL_IMAGE."
                ) from None
        await run_exec("docker", "pull", self.settings.docker_image, timeout=1200)

    async def _new_warm_container(self) -> str:
        name = f"minion-warm-{uuid4().hex[:12]}"
        root = self.settings.workspace_root.resolve()
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            self.POOL_LABEL,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.settings.docker_pids_limit),
            "--memory",
            self.settings.docker_memory,
            "--cpus",
            self.settings.docker_cpus,
            "--network",
            self.settings.docker_network,
            "-v",
            f"{root}:/minion-workspaces",
            "-v",
            f"{self.dep_cache['pip'].resolve()}:/root/.cache/pip",
            "-v",
            f"{self.dep_cache['npm'].resolve()}:/root/.npm",
            "-v",
            f"{self.dep_cache['m2'].resolve()}:/root/.m2",
            "-v",
            f"{self.dep_cache['gradle'].resolve()}:/root/.gradle",
            "-v",
            f"{self.dep_cache['go'].resolve()}:/root/go/pkg/mod",
            self.settings.docker_image,
            "sleep",
            "infinity",
        ]
        await run_exec(*args, timeout=300)
        self._containers.add(name)
        return name

    async def prepare(self) -> None:
        await self._ensure_image()
        try:
            old = await run_exec(
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={self.POOL_LABEL}",
            )
            for name_or_id in old.split():
                try:
                    await run_exec("docker", "rm", "-f", name_or_id)
                except EnvironmentError as exc:
                    log.warning("warm_container_cleanup_failed", error=str(exc))
        except EnvironmentError as exc:
            log.warning("warm_pool_discovery_failed", error=str(exc))

        for _ in range(max(1, self.settings.warm_pool_size)):
            name = await self._new_warm_container()
            await self._pool.put(name)

    async def _claim(self) -> str:
        return await self._pool.get()

    async def allocate(
        self, task_id: str, repositories: list[RepositorySpec]
    ) -> Workspace:
        workspace = await self.local.allocate(task_id, repositories)
        try:
            workspace.container_name = await self._claim()
            workspace.container_workspace_root = (
                f"/minion-workspaces/{workspace.environment_id}"
            )
            return workspace
        except BaseException:
            await self.local.release(workspace)
            raise

    async def attach(
        self, environment_id: str, repositories: list[RepositorySpec]
    ) -> Workspace | None:
        workspace = await self.local.attach(environment_id, repositories)
        if not workspace:
            return None
        workspace.container_name = await self._claim()
        workspace.container_workspace_root = f"/minion-workspaces/{environment_id}"
        return workspace

    async def release(self, workspace: Workspace) -> None:
        container = workspace.container_name
        await self.local.release(workspace)
        if container and container in self._containers:
            await self._pool.put(container)

    async def healthy(self, workspace: Workspace) -> bool:
        if not await self.local.healthy(workspace):
            return False
        if not workspace.container_name:
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
        except EnvironmentError:
            return False

    async def shutdown(self) -> None:
        for container in list(self._containers):
            try:
                await run_exec("docker", "rm", "-f", container)
            except EnvironmentError as exc:
                log.warning("warm_container_shutdown_failed", error=str(exc))
        self._containers.clear()


def build_environment_provider(settings: Settings) -> EnvironmentProvider:
    if settings.environment_provider == "local":
        return LocalEnvironmentProvider(settings)
    if settings.environment_provider == "docker":
        return DockerEnvironmentProvider(settings)
    raise ValueError(
        f"unsupported environment provider {settings.environment_provider!r}; "
        "implement EnvironmentProvider for DevPod/Kubernetes/cloud execution"
    )
