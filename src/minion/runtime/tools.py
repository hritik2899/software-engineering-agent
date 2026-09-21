"""Tool registry exposed to the coding agent."""
from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minion.errors import ToolExecutionError
from minion.runtime.intelligence import RepositoryIntelligence
from minion.runtime.workspace import Workspace


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: str


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]

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
    candidate = (repo / relative).resolve()
    base = repo.resolve()
    if candidate != base and base not in candidate.parents:
        raise ToolExecutionError("path escapes repository")
    return candidate


class ToolRegistry:
    def __init__(
        self,
        workspace: Workspace,
        intelligence: RepositoryIntelligence,
        command_timeout: int = 600,
        update_plan: Callable[[list[str]], Awaitable[None]] | None = None,
        checkpoint_callback: (
            Callable[[str, str, str, str], Awaitable[None]] | None
        ) = None,
    ):
        self.workspace = workspace
        self.intelligence = intelligence
        self.command_timeout = command_timeout
        self.update_plan_callback = update_plan
        self.checkpoint_callback = checkpoint_callback
        self._tools: dict[str, Tool] = {}
        self._register_builtin_tools()

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, f"unknown tool: {name}")
        try:
            return await tool.handler(arguments)
        except (OSError, ValueError, KeyError, ToolExecutionError) as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    def _repo(self, args: dict[str, Any]) -> Path:
        return self.workspace.repo_path(args["repo"])

    async def _command(self, repo_name: str, command: str) -> ToolResult:
        repo = self.workspace.repo_path(repo_name)
        if self.workspace.container_name:
            relative = repo.relative_to(self.workspace.root)
            container_cwd = (
                f"{self.workspace.container_workspace_root}/{relative}"
            )
            process = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "-w",
                container_cwd,
                self.workspace.container_name,
                "sh",
                "-lc",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                False,
                f"command timed out after {self.command_timeout}s",
            )
        output = stdout.decode(errors="replace")
        return ToolResult(process.returncode == 0, output[-30_000:])

    async def _git_output(self, repo_name: str, *args: str) -> tuple[bool, str]:
        repo = self.workspace.repo_path(repo_name)
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        return process.returncode == 0, stdout.decode(errors="replace")

    def _register_builtin_tools(self) -> None:
        async def list_files(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            base = _safe_path(repo, args.get("path", "."))
            depth = int(args.get("max_depth", 3))
            lines: list[str] = []
            base_depth = len(base.parts)
            for path in sorted(base.rglob("*")):
                if ".git" in path.parts or len(path.parts) - base_depth > depth:
                    continue
                suffix = "/" if path.is_dir() else ""
                lines.append(str(path.relative_to(repo)) + suffix)
                if len(lines) >= 500:
                    lines.append("... truncated ...")
                    break
            return ToolResult(True, "\n".join(lines))

        async def read_file(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            path = _safe_path(repo, args["path"])
            lines = path.read_text(errors="replace").splitlines()
            start = max(int(args.get("start_line", 1)), 1)
            end = int(args.get("end_line", start + 300))
            output = "\n".join(
                f"{line_number}: {line}"
                for line_number, line in enumerate(
                    lines[start - 1 : end],
                    start=start,
                )
            )
            return ToolResult(True, output)

        async def write_file(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            path = _safe_path(repo, args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            return ToolResult(
                True,
                f"wrote {path.relative_to(repo)} ({path.stat().st_size} bytes)",
            )

        async def search_code(args: dict[str, Any]) -> ToolResult:
            query = shlex.quote(args["query"])
            return await self._command(
                args["repo"],
                f"git grep -n -I -e {query} || true",
            )

        async def run_command(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], args["command"])

        async def git_diff(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], "git diff --")

        async def git_status(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], "git status --short")

        async def checkpoint(args: dict[str, Any]) -> ToolResult:
            repo_name = args["repo"]
            message = shlex.quote(args.get("message", "agent checkpoint"))
            result = await self._command(
                repo_name,
                f"git add -A && git commit -m {message} --allow-empty && git rev-parse HEAD",
            )
            if not result.ok:
                return result

            commit_sha = result.output.strip().splitlines()[-1]
            base_branch = self.workspace.base_branches.get(repo_name, "main")
            ok, patch = await self._git_output(
                repo_name,
                "diff",
                "--binary",
                f"origin/{base_branch}...HEAD",
            )
            if not ok:
                return ToolResult(False, patch)
            if self.checkpoint_callback is not None:
                await self.checkpoint_callback(
                    repo_name,
                    base_branch,
                    commit_sha,
                    patch,
                )
            return ToolResult(True, f"checkpoint={commit_sha}")

        async def search_index(args: dict[str, Any]) -> ToolResult:
            result = await self.intelligence.retrieve(
                args["query"],
                limit=int(args.get("limit", 8)),
            )
            return ToolResult(True, result or "no indexed matches")

        async def dependency_neighbors(args: dict[str, Any]) -> ToolResult:
            result = self.intelligence.dependency_neighbors(
                args["repo"],
                args["path"],
            )
            return ToolResult(True, json.dumps(result, indent=2))

        async def update_plan(args: dict[str, Any]) -> ToolResult:
            if self.update_plan_callback is None:
                return ToolResult(False, "plan persistence is unavailable")
            plan = [str(item) for item in args["steps"]][:30]
            await self.update_plan_callback(plan)
            return ToolResult(True, f"persisted {len(plan)} plan steps")

        common = {
            "repo": {
                "type": "string",
                "description": "Repository name in this task workspace",
            }
        }

        def schema(
            extra: dict[str, Any],
            required: list[str],
            *,
            include_repo: bool = True,
        ) -> dict[str, Any]:
            properties = {**common, **extra} if include_repo else extra
            required_fields = ["repo", *required] if include_repo else required
            return {
                "type": "object",
                "properties": properties,
                "required": required_fields,
            }

        self._tools = {
            "list_files": Tool(
                "list_files",
                "List repository files.",
                schema({"path": {"type": "string"}, "max_depth": {"type": "integer"}}, []),
                list_files,
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
            ),
            "write_file": Tool(
                "write_file",
                "Replace/create a UTF-8 text file.",
                schema(
                    {"path": {"type": "string"}, "content": {"type": "string"}},
                    ["path", "content"],
                ),
                write_file,
            ),
            "search_code": Tool(
                "search_code",
                "Search tracked source files.",
                schema({"query": {"type": "string"}}, ["query"]),
                search_code,
            ),
            "run_command": Tool(
                "run_command",
                "Run a build/test/formatter command.",
                schema({"command": {"type": "string"}}, ["command"]),
                run_command,
            ),
            "git_diff": Tool("git_diff", "Show uncommitted Git diff.", schema({}, []), git_diff),
            "git_status": Tool("git_status", "Show short Git status.", schema({}, []), git_status),
            "checkpoint": Tool(
                "checkpoint",
                "Create a Git checkpoint and persist its recoverable binary patch.",
                schema({"message": {"type": "string"}}, []),
                checkpoint,
            ),
            "search_index": Tool(
                "search_index",
                "Search the reusable repository context index.",
                schema(
                    {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    ["query"],
                    include_repo=False,
                ),
                search_index,
            ),
            "dependency_neighbors": Tool(
                "dependency_neighbors",
                "Show indexed imports/symbols and possible incoming dependency files.",
                schema({"path": {"type": "string"}}, ["path"]),
                dependency_neighbors,
            ),
            "update_plan": Tool(
                "update_plan",
                "Persist the current implementation plan in the durable session.",
                schema(
                    {"steps": {"type": "array", "items": {"type": "string"}}},
                    ["steps"],
                    include_repo=False,
                ),
                update_plan,
            ),
        }
