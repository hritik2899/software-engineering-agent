"""Agent tool registry: controlled boundary between reasoning and execution.

The model never receives direct filesystem handles, credentials or container access.
It only receives JSON schemas generated here. Read-only tools are marked
parallel_safe; mutating tools stay sequential to preserve deterministic edit order.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minion.errors import ToolExecutionError
from minion.git_auth import safe_child_environment
from minion.runtime.code_index import RepositoryContextIndex
from minion.runtime.policy import CommandPolicy
from minion.runtime.workspace import Workspace


@dataclass(slots=True)
class ToolResult:
    """Normalized result persisted into the durable event stream."""

    ok: bool
    output: str


@dataclass(slots=True)
class Tool:
    """Metadata plus executable handler for one model-visible tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    parallel_safe: bool = False
    mutating: bool = False

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _safe_path(repo: Path, relative: str) -> Path:
    candidate, base = (repo / relative).resolve(), repo.resolve()
    if candidate != base and base not in candidate.parents:
        raise ToolExecutionError("path escapes repository")
    return candidate


class ToolRegistry:
    """Own all built-in repository/shell/Git/index tools for one workspace."""

    def __init__(
        self,
        workspace: Workspace,
        command_timeout: int = 600,
        *,
        cache_root: Path | None = None,
        command_policy_mode: str = "enforce",
    ):
        self.workspace = workspace
        self.command_timeout = command_timeout
        self.index = RepositoryContextIndex(
            cache_root or (workspace.root.parent / "cache")
        )
        self.policy = CommandPolicy(command_policy_mode)
        self._tools: dict[str, Tool] = {}
        self._register_builtin_tools()

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [
            tool.openai_schema()
            for tool in self._tools.values()
        ]

    def is_parallel_safe(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.parallel_safe)

    async def execute(
        self, name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(False, f"unknown tool: {name}")
        try:
            return await tool.handler(arguments)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            FileNotFoundError,
            ToolExecutionError,
        ) as exc:
            return ToolResult(
                False, f"{type(exc).__name__}: {exc}"
            )

    def _repo(self, args: dict[str, Any]) -> Path:
        return self.workspace.repo_path(args["repo"])

    async def _command(
        self, repo_name: str, command: str
    ) -> ToolResult:
        decision = self.policy.evaluate(command)
        if not decision.allowed:
            return ToolResult(
                False,
                f"command blocked by policy: {decision.reason}",
            )

        child_env = safe_child_environment()
        if self.workspace.container_name:
            cwd = self.workspace.container_repo_path(repo_name)
            process = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "-w",
                cwd,
                self.workspace.container_name,
                "sh",
                "-lc",
                command,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            repo = self.workspace.repo_path(repo_name)
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(repo),
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                False,
                f"command timed out after {self.command_timeout}s",
            )

        output = stdout.decode(errors="replace")
        return ToolResult(
            process.returncode == 0,
            output[-30_000:],
        )

    def _register_builtin_tools(self) -> None:
        async def repository_overview(
            args: dict[str, Any]
        ) -> ToolResult:
            payload = await self.index.overview(self._repo(args))
            return ToolResult(
                True, json.dumps(payload, indent=2)
            )

        async def repository_search(
            args: dict[str, Any]
        ) -> ToolResult:
            payload = await self.index.search(
                self._repo(args),
                str(args["query"]),
                limit=int(args.get("limit", 12)),
            )
            return ToolResult(
                True, json.dumps(payload, indent=2)
            )

        async def symbol_context(
            args: dict[str, Any]
        ) -> ToolResult:
            payload = await self.index.symbol_context(
                self._repo(args),
                str(args["symbol"]),
                limit=int(args.get("limit", 20)),
            )
            return ToolResult(
                True, json.dumps(payload, indent=2)
            )

        async def impact_analysis(
            args: dict[str, Any]
        ) -> ToolResult:
            payload = await self.index.impact_analysis(
                self._repo(args),
                str(args["target"]),
                depth=int(args.get("depth", 2)),
            )
            return ToolResult(
                True, json.dumps(payload, indent=2)
            )

        async def dependency_hints(
            args: dict[str, Any]
        ) -> ToolResult:
            payload = await self.index.dependency_hints(
                self._repo(args),
                args["path"],
            )
            return ToolResult(
                True, json.dumps(payload, indent=2)
            )

        async def index_stats(
            args: dict[str, Any]
        ) -> ToolResult:
            payload = await self.index.stats(self._repo(args))
            return ToolResult(
                True, json.dumps(payload, indent=2)
            )

        async def list_files(
            args: dict[str, Any]
        ) -> ToolResult:
            repo = self._repo(args)
            base_path = _safe_path(
                repo, args.get("path", ".")
            )
            depth = max(
                1, min(int(args.get("max_depth", 3)), 8)
            )
            lines: list[str] = []
            base_depth = len(base_path.parts)
            for path in sorted(base_path.rglob("*")):
                if (
                    ".git" in path.parts
                    or len(path.parts) - base_depth > depth
                ):
                    continue
                lines.append(
                    str(path.relative_to(repo))
                    + ("/" if path.is_dir() else "")
                )
                if len(lines) >= 500:
                    lines.append("... truncated ...")
                    break
            return ToolResult(True, "\n".join(lines))

        async def read_file(
            args: dict[str, Any]
        ) -> ToolResult:
            repo = self._repo(args)
            path = _safe_path(repo, args["path"])
            lines = path.read_text(
                errors="replace"
            ).splitlines()
            start = max(
                int(args.get("start_line", 1)), 1
            )
            end = min(
                int(args.get("end_line", start + 300)),
                start + 1000,
            )
            return ToolResult(
                True,
                "\n".join(
                    f"{i}: {line}"
                    for i, line in enumerate(
                        lines[start - 1 : end],
                        start=start,
                    )
                ),
            )

        async def write_file(
            args: dict[str, Any]
        ) -> ToolResult:
            repo = self._repo(args)
            path = _safe_path(repo, args["path"])
            content = str(args["content"])
            if len(content.encode()) > 2_000_000:
                return ToolResult(
                    False,
                    "write rejected: file content exceeds 2 MB",
                )
            path.parent.mkdir(
                parents=True, exist_ok=True
            )
            path.write_text(content)
            return ToolResult(
                True,
                f"wrote {path.relative_to(repo)} "
                f"({path.stat().st_size} bytes)",
            )

        async def apply_patch(
            args: dict[str, Any]
        ) -> ToolResult:
            repo_name = args["repo"]
            repo = self._repo(args)
            patch = str(args["patch"])
            if not patch.strip():
                return ToolResult(False, "patch is empty")
            if len(patch.encode()) > 500_000:
                return ToolResult(
                    False,
                    "patch rejected: patch exceeds 500 KB",
                )

            patch_file = repo / ".minion-agent.patch"
            patch_file.write_text(patch)
            try:
                return await self._command(
                    repo_name,
                    "git apply --check .minion-agent.patch "
                    "&& git apply --whitespace=nowarn "
                    ".minion-agent.patch",
                )
            finally:
                patch_file.unlink(missing_ok=True)

        async def search_code(
            args: dict[str, Any]
        ) -> ToolResult:
            query = shlex.quote(args["query"])
            return await self._command(
                args["repo"],
                f"git grep -n -I -e {query} || true",
            )

        async def run_command(
            args: dict[str, Any]
        ) -> ToolResult:
            return await self._command(
                args["repo"], args["command"]
            )

        async def git_diff(
            args: dict[str, Any]
        ) -> ToolResult:
            return await self._command(
                args["repo"], "git diff --"
            )

        async def git_status(
            args: dict[str, Any]
        ) -> ToolResult:
            return await self._command(
                args["repo"], "git status --short"
            )

        async def checkpoint(
            args: dict[str, Any]
        ) -> ToolResult:
            message = shlex.quote(
                args.get(
                    "message", "agent checkpoint"
                )
            )
            return await self._command(
                args["repo"],
                f"git add -A && git commit -m {message} "
                "--allow-empty && git rev-parse HEAD",
            )

        common = {
            "repo": {
                "type": "string",
                "description": (
                    "Repository name in the task workspace"
                ),
            }
        }

        def schema(
            extra: dict[str, Any],
            required: list[str],
        ) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {**common, **extra},
                "required": ["repo", *required],
            }

        self._tools = {
            "repository_overview": Tool(
                "repository_overview",
                (
                    "Return the pre-indexed ranked repository "
                    "map and index statistics."
                ),
                schema({}, []),
                repository_overview,
                parallel_safe=True,
            ),
            "repository_search": Tool(
                "repository_search",
                (
                    "Search the persistent repository index by "
                    "path, symbol, import or code preview."
                ),
                schema(
                    {
                        "query": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    ["query"],
                ),
                repository_search,
                parallel_safe=True,
            ),
            "symbol_context": Tool(
                "symbol_context",
                (
                    "Find symbol definitions plus dependency "
                    "and dependent files."
                ),
                schema(
                    {
                        "symbol": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    ["symbol"],
                ),
                symbol_context,
                parallel_safe=True,
            ),
            "impact_analysis": Tool(
                "impact_analysis",
                (
                    "Estimate reverse-dependency blast radius "
                    "for a file or symbol."
                ),
                schema(
                    {
                        "target": {"type": "string"},
                        "depth": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 5,
                        },
                    },
                    ["target"],
                ),
                impact_analysis,
                parallel_safe=True,
            ),
            "dependency_hints": Tool(
                "dependency_hints",
                (
                    "Return imports and resolved in-repository "
                    "dependencies for a file."
                ),
                schema(
                    {"path": {"type": "string"}},
                    ["path"],
                ),
                dependency_hints,
                parallel_safe=True,
            ),
            "index_stats": Tool(
                "index_stats",
                (
                    "Return current repository-index coverage "
                    "and reuse statistics."
                ),
                schema({}, []),
                index_stats,
                parallel_safe=True,
            ),
            "list_files": Tool(
                "list_files",
                "List files under a repository path.",
                schema(
                    {
                        "path": {"type": "string"},
                        "max_depth": {"type": "integer"},
                    },
                    [],
                ),
                list_files,
                parallel_safe=True,
            ),
            "read_file": Tool(
                "read_file",
                "Read a text file with line numbers.",
                schema(
                    {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    ["path"],
                ),
                read_file,
                parallel_safe=True,
            ),
            "write_file": Tool(
                "write_file",
                (
                    "Replace/create a UTF-8 text file. Prefer "
                    "apply_patch for existing files."
                ),
                schema(
                    {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    ["path", "content"],
                ),
                write_file,
                mutating=True,
            ),
            "apply_patch": Tool(
                "apply_patch",
                (
                    "Apply a unified Git patch after git-apply "
                    "validation."
                ),
                schema(
                    {"patch": {"type": "string"}},
                    ["patch"],
                ),
                apply_patch,
                mutating=True,
            ),
            "search_code": Tool(
                "search_code",
                "Search tracked source files with git grep.",
                schema(
                    {"query": {"type": "string"}},
                    ["query"],
                ),
                search_code,
                parallel_safe=True,
            ),
            "run_command": Tool(
                "run_command",
                (
                    "Run a build/test/formatter command subject "
                    "to deterministic safety policy."
                ),
                schema(
                    {"command": {"type": "string"}},
                    ["command"],
                ),
                run_command,
                mutating=True,
            ),
            "git_diff": Tool(
                "git_diff",
                "Show uncommitted Git diff.",
                schema({}, []),
                git_diff,
                parallel_safe=True,
            ),
            "git_status": Tool(
                "git_status",
                "Show short Git status.",
                schema({}, []),
                git_status,
                parallel_safe=True,
            ),
            "checkpoint": Tool(
                "checkpoint",
                "Create a durable Git checkpoint commit.",
                schema(
                    {"message": {"type": "string"}},
                    [],
                ),
                checkpoint,
                mutating=True,
            ),
        }
