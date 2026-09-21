"""Tool registry exposed to the coding agent."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from minion.errors import ToolExecutionError
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
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters
        }}


def _safe_path(repo: Path, relative: str) -> Path:
    candidate, base = (repo / relative).resolve(), repo.resolve()
    if candidate != base and base not in candidate.parents:
        raise ToolExecutionError("path escapes repository")
    return candidate


class ToolRegistry:
    def __init__(self, workspace: Workspace, command_timeout: int = 600):
        self.workspace = workspace
        self.command_timeout = command_timeout
        self._tools: dict[str, Tool] = {}
        self._register_builtin_tools()

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(False, f"unknown tool: {name}")
        try:
            return await tool.handler(arguments)
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    def _repo(self, args: dict[str, Any]) -> Path:
        return self.workspace.repo_path(args["repo"])

    async def _command(self, repo_name: str, command: str) -> ToolResult:
        repo = self.workspace.repo_path(repo_name)
        if self.workspace.container_name:
            relative = repo.relative_to(self.workspace.root)
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", "-w", f"/workspace/{relative}",
                self.workspace.container_name, "sh", "-lc", command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command, cwd=str(repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.command_timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(False, f"command timed out after {self.command_timeout}s")
        output = stdout.decode(errors="replace")
        return ToolResult(process.returncode == 0, output[-30000:])

    def _register_builtin_tools(self) -> None:
        async def list_files(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            base = _safe_path(repo, args.get("path", "."))
            depth = int(args.get("max_depth", 3))
            lines, base_depth = [], len(base.parts)
            for path in sorted(base.rglob("*")):
                if ".git" in path.parts or len(path.parts) - base_depth > depth:
                    continue
                lines.append(str(path.relative_to(repo)) + ("/" if path.is_dir() else ""))
                if len(lines) >= 500:
                    lines.append("... truncated ...")
                    break
            return ToolResult(True, "\n".join(lines))

        async def read_file(args: dict[str, Any]) -> ToolResult:
            repo, path = self._repo(args), None
            path = _safe_path(repo, args["path"])
            lines = path.read_text(errors="replace").splitlines()
            start = max(int(args.get("start_line", 1)), 1)
            end = int(args.get("end_line", start + 300))
            return ToolResult(True, "\n".join(
                f"{i}: {line}" for i, line in enumerate(lines[start - 1:end], start=start)
            ))

        async def write_file(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            path = _safe_path(repo, args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            return ToolResult(True, f"wrote {path.relative_to(repo)} ({path.stat().st_size} bytes)")

        async def search_code(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], f"git grep -n -I -e {__import__('shlex').quote(args['query'])} || true")

        async def run_command(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], args["command"])

        async def git_diff(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], "git diff --")

        async def git_status(args: dict[str, Any]) -> ToolResult:
            return await self._command(args["repo"], "git status --short")

        async def checkpoint(args: dict[str, Any]) -> ToolResult:
            import shlex
            message = shlex.quote(args.get("message", "agent checkpoint"))
            return await self._command(
                args["repo"],
                f"git add -A && git commit -m {message} --allow-empty && git rev-parse HEAD",
            )

        common = {"repo": {"type": "string", "description": "Repository name in task workspace"}}
        def schema(extra: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {"type": "object", "properties": {**common, **extra}, "required": ["repo", *required]}

        self._tools = {
            "list_files": Tool("list_files", "List repository files.", schema(
                {"path": {"type": "string"}, "max_depth": {"type": "integer"}}, []
            ), list_files),
            "read_file": Tool("read_file", "Read a text file with line numbers.", schema(
                {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]
            ), read_file),
            "write_file": Tool("write_file", "Replace/create a UTF-8 text file.", schema(
                {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]
            ), write_file),
            "search_code": Tool("search_code", "Search tracked source files.", schema(
                {"query": {"type": "string"}}, ["query"]
            ), search_code),
            "run_command": Tool("run_command", "Run a build/test/formatter command.", schema(
                {"command": {"type": "string"}}, ["command"]
            ), run_command),
            "git_diff": Tool("git_diff", "Show uncommitted Git diff.", schema({}, []), git_diff),
            "git_status": Tool("git_status", "Show short Git status.", schema({}, []), git_status),
            "checkpoint": Tool("checkpoint", "Create a durable Git checkpoint commit.", schema(
                {"message": {"type": "string"}}, []
            ), checkpoint),
        }
