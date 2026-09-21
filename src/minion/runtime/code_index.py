"""Reusable repository context cache keyed by repository HEAD.

This is intentionally lightweight: it gives the agent a fast architectural
overview without rescanning the repository on every task. It can later be
replaced by a richer graph/vector index without changing the agent interface.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


class RepositoryContextIndex:
    def __init__(self, cache_root: Path):
        self.root = cache_root / "repository_context"
        self.root.mkdir(parents=True, exist_ok=True)

    async def _git(self, repo: Path, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stdout.decode(errors="replace"))
        return stdout.decode(errors="replace")

    async def _key(self, repo: Path) -> tuple[str, str]:
        head = (await self._git(repo, "rev-parse", "HEAD")).strip()
        raw = f"{repo.resolve()}::{head}".encode()
        return hashlib.sha256(raw).hexdigest(), head

    async def overview(self, repo: Path) -> dict[str, Any]:
        key, head = await self._key(repo)
        target = self.root / f"{key}.json"
        if target.exists():
            return json.loads(target.read_text())

        files = [
            line
            for line in (await self._git(repo, "ls-files")).splitlines()
            if line.strip()
        ]
        extensions = Counter(
            Path(path).suffix.lower() or "<no-extension>" for path in files
        )
        top_dirs = Counter(
            Path(path).parts[0] if len(Path(path).parts) > 1 else "<root>"
            for path in files
        )
        important_names = {
            "README.md",
            "pyproject.toml",
            "package.json",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "go.mod",
            "Cargo.toml",
            "Makefile",
            "docker-compose.yml",
            "Dockerfile",
        }
        important = [path for path in files if Path(path).name in important_names]

        payload = {
            "head": head,
            "file_count": len(files),
            "extensions": dict(extensions.most_common(20)),
            "top_level_areas": dict(top_dirs.most_common(30)),
            "important_files": important[:100],
            "sample_files": files[:200],
        }
        target.write_text(json.dumps(payload, indent=2))
        return payload

    async def dependency_hints(self, repo: Path, relative_path: str) -> dict[str, Any]:
        path = (repo / relative_path).resolve()
        base = repo.resolve()
        if path != base and base not in path.parents:
            raise ValueError("path escapes repository")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(relative_path)

        lines = path.read_text(errors="replace").splitlines()
        hints: list[str] = []
        for line in lines:
            stripped = line.strip()
            if (
                stripped.startswith("import ")
                or stripped.startswith("from ")
                or " require(" in stripped
                or " from "" in stripped
                or " from '" in stripped
                or stripped.startswith("#include")
            ):
                hints.append(stripped)
            if len(hints) >= 200:
                break

        return {
            "file": relative_path,
            "imports_and_dependency_hints": hints,
            "note": (
                "These are syntactic dependency hints, not a complete semantic "
                "call graph. A graph service can replace this index behind the "
                "same context/tool boundary."
            ),
        }
