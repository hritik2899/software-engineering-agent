"""Reusable repository context and lightweight dependency index.

The index is logically partitioned by repository URL + commit SHA. Different tasks
on the same immutable revision can reuse it, while a new commit automatically gets
a new cache key. This models the shared-but-partitioned context pool discussed in
the architecture tutorial.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from minion.config import Settings
from minion.runtime.workspace import Workspace


_SOURCE_SUFFIXES = {
    ".py",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".rs",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".proto",
}

_SYMBOL_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(
        r"^\s*(?:public|private|protected|static|final|synchronized|abstract|\s)+"
        r"[\w<>,.?\[\]]+\s+([A-Za-z_]\w*)\s*\(",
        re.MULTILINE,
    ),
    re.compile(r"\b(?:function|class|interface|type)\s+([A-Za-z_$][\w$]*)"),
]

_IMPORT_PATTERNS = [
    re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE),
    re.compile(r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    re.compile(r"require\(['\"]([^'\"]+)['\"]\)"),
    re.compile(r"^\s*import\s+[\w.]+\.?([\w.*]+)?;", re.MULTILINE),
]


@dataclass(slots=True)
class IndexedFile:
    path: str
    symbols: list[str]
    imports: list[str]
    search_text: str


@dataclass(slots=True)
class RepoPartition:
    repo: str
    origin: str
    commit_sha: str
    files: list[IndexedFile]


async def _git(repo: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return ""
    return stdout.decode(errors="replace").strip()


class RepositoryIntelligence:
    def __init__(self, settings: Settings, workspace: Workspace):
        self.settings = settings
        self.workspace = workspace
        self.cache_root = settings.cache_root / "code-index"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.partitions: dict[str, RepoPartition] = {}

    async def prepare(self) -> None:
        if not self.settings.code_index_enabled:
            return
        for name, repo in self.workspace.repositories.items():
            self.partitions[name] = await self._load_or_build(name, repo)

    async def _load_or_build(self, name: str, repo: Path) -> RepoPartition:
        origin = await _git(repo, "remote", "get-url", "origin")
        commit_sha = await _git(repo, "rev-parse", "HEAD")
        identity = f"{origin}\0{commit_sha}".encode()
        cache_path = self.cache_root / f"{hashlib.sha256(identity).hexdigest()}.json"

        if cache_path.exists():
            data = json.loads(cache_path.read_text())
            return RepoPartition(
                repo=data["repo"],
                origin=data["origin"],
                commit_sha=data["commit_sha"],
                files=[IndexedFile(**item) for item in data["files"]],
            )

        tracked = await _git(repo, "ls-files")
        files: list[IndexedFile] = []
        for relative in tracked.splitlines()[: self.settings.code_index_max_files]:
            path = repo / relative
            if path.suffix.lower() not in _SOURCE_SUFFIXES or not path.is_file():
                continue
            if path.stat().st_size > self.settings.code_index_max_file_bytes:
                continue
            text = path.read_text(errors="replace")
            symbols = sorted(
                {
                    match.group(1)
                    for pattern in _SYMBOL_PATTERNS
                    for match in pattern.finditer(text)
                    if match.group(1)
                }
            )[:300]
            imports = sorted(
                {
                    match.group(1)
                    for pattern in _IMPORT_PATTERNS
                    for match in pattern.finditer(text)
                    if match.group(1)
                }
            )[:300]
            files.append(
                IndexedFile(
                    path=relative,
                    symbols=symbols,
                    imports=imports,
                    search_text=text[:20_000].lower(),
                )
            )

        partition = RepoPartition(name, origin, commit_sha, files)
        cache_path.write_text(
            json.dumps(
                {
                    "repo": partition.repo,
                    "origin": partition.origin,
                    "commit_sha": partition.commit_sha,
                    "files": [asdict(item) for item in partition.files],
                }
            )
        )
        return partition

    async def retrieve(self, query: str, limit: int = 8) -> str:
        if not self.partitions:
            return ""
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
            if token.lower() not in {"the", "and", "for", "with", "from", "this", "that"}
        }
        ranked: list[tuple[int, str, IndexedFile]] = []
        for repo, partition in self.partitions.items():
            for item in partition.files:
                path_text = item.path.lower()
                score = sum(8 for token in tokens if token in path_text)
                score += sum(
                    4
                    for token in tokens
                    if any(token in symbol.lower() for symbol in item.symbols)
                )
                score += sum(min(item.search_text.count(token), 5) for token in tokens)
                if score:
                    ranked.append((score, repo, item))
        ranked.sort(key=lambda row: row[0], reverse=True)

        sections: list[str] = []
        for score, repo_name, item in ranked[:limit]:
            path = self.workspace.repo_path(repo_name) / item.path
            text = path.read_text(errors="replace").splitlines()
            matched_line = 0
            for index, line in enumerate(text):
                lowered = line.lower()
                if any(token in lowered for token in tokens):
                    matched_line = index
                    break
            start = max(0, matched_line - 8)
            end = min(len(text), matched_line + 22)
            snippet = "\n".join(
                f"{line_no + 1}: {text[line_no]}" for line_no in range(start, end)
            )
            sections.append(
                f"[{repo_name}:{item.path} score={score}]\n"
                f"symbols={item.symbols[:20]}\n"
                f"imports={item.imports[:20]}\n{snippet}"
            )
        return "\n\n".join(sections)

    def dependency_neighbors(self, repo: str, path: str) -> dict[str, object]:
        partition = self.partitions.get(repo)
        if partition is None:
            return {"error": f"unknown indexed repository {repo}"}
        target = next((item for item in partition.files if item.path == path), None)
        if target is None:
            return {"error": f"{path} is not indexed"}
        incoming = [
            item.path
            for item in partition.files
            if any(
                imported in target.path or Path(target.path).stem in imported
                for imported in item.imports
            )
        ]
        return {
            "repo": repo,
            "path": path,
            "imports": target.imports,
            "symbols": target.symbols,
            "possible_incoming_files": incoming[:100],
        }
