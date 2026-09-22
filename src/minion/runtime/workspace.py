"""Sandbox/workspace lifecycle and warm execution pool.

A Workspace maps one environment_id to one or more task-specific Git worktrees.
Repository mirrors, dependency caches and Docker warm slots remove repeated cold
work. Each warm container mounts only its own task slot so concurrent agents cannot
browse one another's source trees.

The local provider is for trusted development; the production reference path uses
the resource-limited Docker provider.
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

from filelock import FileLock

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
    slot_id: str | None = None

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


@dataclass(slots=True)
class WarmSlot:
    slot_id: str
    container_name: str
    host_root: Path


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
    async def release(
        self, workspace: Workspace, *, destroy_workspace: bool = True
    ) -> None: ...

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
            lock = FileLock(str(self.repo_cache / f"{mirror_key}.lock"), timeout=120)
            await asyncio.to_thread(lock.acquire)
            try:
                if mirror.exists():
                    await run_exec(
                        "git",
                        "remote",
                        "update",
                        "--prune",
                        cwd=mirror,
                        env=auth_env,
                    )
                else:
                    await run_exec(
                        "git",
                        "clone",
                        "--mirror",
                        spec.url,
                        str(mirror),
                        env=auth_env,
                    )
            finally:
                await asyncio.to_thread(lock.release)

            destination = root / name
            # Clone from the already-updated local mirror so repeated tasks avoid
            # network transfer of repository objects.
            await run_exec(
                "git",
                "clone",
                "--branch",
                spec.base_branch,
                str(mirror),
                str(destination),
            )
            await run_exec(
                "git", "remote", "set-url", "origin", spec.url, cwd=destination
            )
            await run_exec(
                "git", "checkout", "-b", f"agent/{task_id}", cwd=destination
            )
            await run_exec(
                "git", "config", "user.name", "Minion Agent", cwd=destination
            )
            await run_exec(
                "git",
                "config",
                "user.email",
                "minion-agent@localhost",
                cwd=destination,
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

    async def release(
        self, workspace: Workspace, *, destroy_workspace: bool = True
    ) -> None:
        if destroy_workspace:
            shutil.rmtree(workspace.root, ignore_errors=True)

    async def healthy(self, workspace: Workspace) -> bool:
        return workspace.root.exists() and all(
            path.exists() for path in workspace.repositories.values()
        )

    async def shutdown(self) -> None:
        return None


class DockerEnvironmentProvider(EnvironmentProvider):
    """Warm containers with one isolated host slot per container.

    A container can only see its own slot. Source trees from concurrent tasks are
    therefore not visible across sandboxes, while dependency caches remain shared.
    """

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

        self.warm_root = settings.cache_root / "warm_slots"
        self.warm_root.mkdir(parents=True, exist_ok=True)
        self._pool: asyncio.Queue[WarmSlot] = asyncio.Queue()
        self._slots: dict[str, WarmSlot] = {}

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

    def _archive_stale_slot_workspaces(self) -> None:
        for slot_dir in self.warm_root.iterdir():
            if not slot_dir.is_dir():
                continue
            for child in slot_dir.iterdir():
                if not child.is_dir() or not child.name.startswith("env_"):
                    continue
                target = self.settings.workspace_root / child.name
                if target.exists():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    shutil.move(str(child), str(target))
            shutil.rmtree(slot_dir, ignore_errors=True)

    async def _new_slot(self) -> WarmSlot:
        slot_id = uuid4().hex[:12]
        slot_root = self.warm_root / slot_id
        slot_root.mkdir(parents=True, exist_ok=False)
        name = f"minion-warm-{slot_id}"
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
            f"{slot_root.resolve()}:/workspace",
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
        slot = WarmSlot(slot_id, name, slot_root)
        self._slots[slot_id] = slot
        return slot

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

        self._archive_stale_slot_workspaces()
        for _ in range(max(1, self.settings.warm_pool_size)):
            await self._pool.put(await self._new_slot())

    async def _claim(self) -> WarmSlot:
        return await self._pool.get()

    async def allocate(
        self, task_id: str, repositories: list[RepositorySpec]
    ) -> Workspace:
        slot = await self._claim()
        environment_id = new_id("env")
        root = slot.host_root / environment_id
        root.mkdir(parents=True, exist_ok=False)
        try:
            repos = await self.local._populate(root, task_id, repositories)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            await self._pool.put(slot)
            raise

        return Workspace(
            environment_id=environment_id,
            root=root,
            repositories=repos,
            container_name=slot.container_name,
            container_workspace_root=f"/workspace/{environment_id}",
            slot_id=slot.slot_id,
        )

    async def attach(
        self, environment_id: str, repositories: list[RepositorySpec]
    ) -> Workspace | None:
        persistent = self.settings.workspace_root / environment_id
        if not persistent.exists():
            return None

        slot = await self._claim()
        target = slot.host_root / environment_id
        try:
            shutil.move(str(persistent), str(target))
            repos = {repo_name(spec): target / repo_name(spec) for spec in repositories}
            workspace = Workspace(
                environment_id=environment_id,
                root=target,
                repositories=repos,
                container_name=slot.container_name,
                container_workspace_root=f"/workspace/{environment_id}",
                slot_id=slot.slot_id,
            )
            if await self.healthy(workspace):
                return workspace
            raise EnvironmentError("recovered Docker workspace is unhealthy")
        except BaseException:
            if target.exists() and not persistent.exists():
                shutil.move(str(target), str(persistent))
            await self._pool.put(slot)
            raise

    async def release(
        self, workspace: Workspace, *, destroy_workspace: bool = True
    ) -> None:
        slot = self._slots.get(workspace.slot_id or "")
        if destroy_workspace:
            shutil.rmtree(workspace.root, ignore_errors=True)
        else:
            target = self.settings.workspace_root / workspace.environment_id
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if workspace.root.exists():
                shutil.move(str(workspace.root), str(target))

        if slot:
            await self._pool.put(slot)

    async def healthy(self, workspace: Workspace) -> bool:
        if not workspace.root.exists() or not all(
            path.exists() for path in workspace.repositories.values()
        ):
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
        for slot in list(self._slots.values()):
            try:
                await run_exec("docker", "rm", "-f", slot.container_name)
            except EnvironmentError as exc:
                log.warning("warm_container_shutdown_failed", error=str(exc))
        self._slots.clear()


def build_environment_provider(settings: Settings) -> EnvironmentProvider:
    if settings.environment_provider == "local":
        return LocalEnvironmentProvider(settings)
    if settings.environment_provider == "docker":
        return DockerEnvironmentProvider(settings)
    raise ValueError(
        f"unsupported environment provider {settings.environment_provider!r}; "
        "implement EnvironmentProvider for DevPod/Kubernetes/cloud execution"
    )
