"""Tool registry exposed to the coding agent.

All filesystem paths are confined to an allocated repository. Shell execution is
allowed because coding agents need compilers/test runners; isolation is therefore
an environment responsibility, not an LLM responsibility.
"""
from __future__ import annotations

import asyncio
import json
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

    def _register_builtin_tools(self) -> None:
        async def list_files(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            base = _safe_path(repo, args.get("path", "."))
            depth = int(args.get("max_depth", 3))
            lines: list[str] = []
            base_depth = len(base.parts)
            for path in sorted(base.rglob("*")):
                if ".git" in path.parts:
                    continue
                if len(path.parts) - base_depth > depth:
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
            text = path.read_text(errors="replace")
            start = max(int(args.get("start_line", 1)), 1)
            end = int(args.get("end_line", start + 300))
            lines = text.splitlines()
            selected = lines[start - 1 : end]
            numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=start)]
            return ToolResult(True, "\n".join(numbered))

        async def write_file(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            path = _safe_path(repo, args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            return ToolResult(True, f"wrote {path.relative_to(repo)} ({path.stat().st_size} bytes)")

        async def search_code(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            query = args["query"]
            process = await asyncio.create_subprocess_exec(
                "git",
                "grep",
                "-n",
                "-I",
                "-e",
                query,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await process.communicate()
            output = stdout.decode(errors="replace")
            # git grep returns 1 when there are no matches.
            return ToolResult(process.returncode in (0, 1), output[:30000] or "no matches")

        async def run_command(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            command = args["command"]
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(), timeout=self.command_timeout
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                return ToolResult(False, f"command timed out after {self.command_timeout}s")
            output = stdout.decode(errors="replace")
            return ToolResult(process.returncode == 0, output[-30000:])

        async def git_diff(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            process = await asyncio.create_subprocess_exec(
                "git", "diff", "--", cwd=str(repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await process.communicate()
            return ToolResult(process.returncode == 0, stdout.decode(errors="replace")[-40000:])

        async def git_status(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            process = await asyncio.create_subprocess_exec(
                "git", "status", "--short", cwd=str(repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await process.communicate()
            return ToolResult(process.returncode == 0, stdout.decode(errors="replace"))

        async def checkpoint(args: dict[str, Any]) -> ToolResult:
            repo = self._repo(args)
            message = args.get("message", "agent checkpoint")
            # Checkpoint commits make workspace replacement recoverable.
            for command in (["git", "add", "-A"], ["git", "commit", "-m", message, "--allow-empty"]):
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                if process.returncode != 0:
                    return ToolResult(False, stdout.decode(errors="replace"))
            process = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD", cwd=str(repo),
                stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            return ToolResult(True, f"checkpoint={stdout.decode().strip()}")

        common_repo = {
            "repo": {"type": "string", "description": "Repository name in this task workspace"}
        }
        self._tools = {
            "list_files": Tool("list_files", "List repository files.", {
                "type": "object",
                "properties": {**common_repo, "path": {"type": "string"}, "max_depth": {"type": "integer"}},
                "required": ["repo"],
            }, list_files),
            "read_file": Tool("read_file", "Read a text file with line numbers.", {
                "type": "object",
                "properties": {**common_repo, "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
                "required": ["repo", "path"],
            }, read_file),
            "write_file": Tool("write_file", "Replace/create a UTF-8 text file.", {
                "type": "object",
                "properties": {**common_repo, "path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["repo", "path", "content"],
            }, write_file),
            "search_code": Tool("search_code", "Search tracked source files using git grep.", {
                "type": "object",
                "properties": {**common_repo, "query": {"type": "string"}},
                "required": ["repo", "query"],
            }, search_code),
            "run_command": Tool("run_command", "Run a build, test, formatter or repository command.", {
                "type": "object",
                "properties": {**common_repo, "command": {"type": "string"}},
                "required": ["repo", "command"],
            }, run_command),
            "git_diff": Tool("git_diff", "Show uncommitted Git diff.", {
                "type": "object", "properties": common_repo, "required": ["repo"]
            }, git_diff),
            "git_status": Tool("git_status", "Show short Git status.", {
                "type": "object", "properties": common_repo, "required": ["repo"]
            }, git_status),
            "checkpoint": Tool("checkpoint", "Create a durable local Git checkpoint commit.", {
                "type": "object",
                "properties": {**common_repo, "message": {"type": "string"}},
                "required": ["repo"],
            }, checkpoint),
        }
