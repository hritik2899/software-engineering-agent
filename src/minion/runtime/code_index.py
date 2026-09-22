"""Content-addressed repository intelligence index.

The index is deliberately derived data: Git remains the source of truth, while this
module turns a checked-out repository into reusable, task-independent navigation
metadata. The design combines two ideas that have proven useful in mature coding
agents:

* content-addressing: unchanged files reuse the exact same parsed object across
  branches/commits instead of being re-indexed;
* ranked repository maps: files/symbols that sit on important dependency paths rank
  higher, so the agent can inspect the right area before reading large amounts of code.

No LLM is required to build the index. That keeps pre-indexing deterministic,
inexpensive, and safe to run before the coding agent starts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock


_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts",
    ".go", ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".rb",
    ".php", ".swift", ".scala", ".sql", ".proto", ".graphql", ".md", ".mdx",
    ".toml", ".yaml", ".yml", ".json", ".xml", ".gradle", ".sh", ".bash",
}
_ALWAYS_TEXT_NAMES = {
    "Dockerfile", "Makefile", "BUILD", "WORKSPACE", "go.mod", "go.sum",
    "pom.xml", "package.json", "pyproject.toml", "requirements.txt",
}
_MAX_INDEXED_FILE_BYTES = 1_000_000


@dataclass(slots=True)
class IndexedRepository:
    """Small immutable handle returned after ensuring an index exists."""

    repository_key: str
    head: str
    manifest_path: Path
    parsed_files: int
    reused_files: int


class RepositoryContextIndex:
    """Build and query a persistent repository map.

    Storage layout under cache_root/repository_index:

      <repo-key>/objects/<content-sha256>.json
      <repo-key>/manifests/<git-head>.json
      <repo-key>/latest.json

    Objects are keyed by file bytes, so a new commit only parses files whose
    contents have never been seen before.
    """

    def __init__(self, cache_root: Path):
        self.root = cache_root / "repository_index"
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

    async def _identity(self, repo: Path) -> tuple[str, str, str]:
        head = (await self._git(repo, "rev-parse", "HEAD")).strip()
        try:
            remote = (
                await self._git(repo, "config", "--get", "remote.origin.url")
            ).strip()
        except RuntimeError:
            remote = f"local:{repo.resolve()}"
        key = hashlib.sha256(remote.encode()).hexdigest()
        return key, remote, head

    @staticmethod
    def _is_indexable(path: Path) -> bool:
        return (
            path.name in _ALWAYS_TEXT_NAMES
            or path.suffix.lower() in _TEXT_SUFFIXES
        )

    @staticmethod
    def _content_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _extract_symbols(path: str, text: str) -> list[dict[str, Any]]:
        suffix = Path(path).suffix.lower()
        patterns: list[tuple[str, re.Pattern[str]]] = []

        if suffix in {".py", ".pyi"}:
            patterns = [
                ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)")),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("
                    ),
                ),
            ]
        elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
            patterns = [
                (
                    "class",
                    re.compile(
                        r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"
                    ),
                ),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:export\s+)?(?:async\s+)?function\s+"
                        r"([A-Za-z_$][\w$]*)\s*\("
                    ),
                ),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:export\s+)?(?:const|let|var)\s+"
                        r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("
                    ),
                ),
            ]
        elif suffix == ".go":
            patterns = [
                ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+")),
                (
                    "function",
                    re.compile(
                        r"^\s*func\s+(?:\([^)]*\)\s*)?"
                        r"([A-Za-z_]\w*)\s*\("
                    ),
                ),
            ]
        elif suffix in {".java", ".kt", ".kts", ".cs", ".scala"}:
            patterns = [
                (
                    "type",
                    re.compile(
                        r"^\s*(?:(?:public|private|protected|internal|abstract|"
                        r"final|sealed|open|static)\s+)*(?:class|interface|enum|"
                        r"record|object)\s+([A-Za-z_]\w*)"
                    ),
                ),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:(?:public|private|protected|internal|static|"
                        r"final|suspend|async|override)\s+)*"
                        r"(?:[\w<>\[\],.?]+\s+)+([A-Za-z_]\w*)\s*"
                        r"\([^;]*\)\s*(?:\{|=)?\s*$"
                    ),
                ),
            ]
        elif suffix in {
            ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs", ".swift"
        }:
            patterns = [
                (
                    "type",
                    re.compile(
                        r"^\s*(?:class|struct|enum|trait|protocol)\s+"
                        r"([A-Za-z_]\w*)"
                    ),
                ),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:pub\s+)?(?:async\s+)?(?:fn|func)\s+"
                        r"([A-Za-z_]\w*)\s*\("
                    ),
                ),
            ]

        symbols: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in patterns:
                match = pattern.search(line)
                if match:
                    symbols.append(
                        {
                            "name": match.group(1),
                            "kind": kind,
                            "line": line_no,
                            "signature": line.strip()[:300],
                        }
                    )
                    break
            if len(symbols) >= 500:
                break
        return symbols

    @staticmethod
    def _extract_imports(path: str, text: str) -> list[str]:
        suffix = Path(path).suffix.lower()
        found: list[str] = []

        for line in text.splitlines():
            stripped = line.strip()
            candidates: list[str] = []
            if suffix in {".py", ".pyi"}:
                match = re.match(r"from\s+([.\w]+)\s+import\s+", stripped)
                if match:
                    candidates.append(match.group(1))
                match = re.match(r"import\s+([.\w]+)", stripped)
                if match:
                    candidates.extend(
                        part.strip().split(" as ")[0]
                        for part in match.group(1).split(",")
                    )
            elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
                match = re.search(
                    r"""(?:from\s+|require\()\s*['"]([^'"]+)['"]""",
                    stripped,
                )
                if match:
                    candidates.append(match.group(1))
            elif suffix == ".go":
                match = re.match(r'import\s+"([^"]+)"', stripped)
                if match:
                    candidates.append(match.group(1))
            elif suffix in {".java", ".kt", ".kts", ".scala"}:
                match = re.match(r"import\s+([\w.*]+)", stripped)
                if match:
                    candidates.append(match.group(1))
            elif suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
                match = re.match(
                    r'#include\s+["<]([^">]+)[">]',
                    stripped,
                )
                if match:
                    candidates.append(match.group(1))

            for candidate in candidates:
                candidate = candidate.strip()
                if candidate and candidate not in found:
                    found.append(candidate)
            if len(found) >= 300:
                break
        return found

    async def _tracked_files(self, repo: Path) -> list[str]:
        return [
            line
            for line in (await self._git(repo, "ls-files")).splitlines()
            if line.strip()
        ]

    def _parse_object(
        self, repo: Path, relative: str
    ) -> tuple[str, dict[str, Any]] | None:
        path = repo / relative
        if not self._is_indexable(path) or not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) > _MAX_INDEXED_FILE_BYTES or b"\x00" in data[:8192]:
            return None

        digest = self._content_hash(data)
        text = data.decode(errors="replace")
        facts = {
            "path": relative,
            "content_hash": digest,
            "size": len(data),
            "symbols": self._extract_symbols(relative, text),
            "imports": self._extract_imports(relative, text),
            "preview": "\n".join(text.splitlines()[:40])[:4000],
        }
        return digest, facts

    @staticmethod
    def _resolve_dependency_edges(
        files: dict[str, dict[str, Any]],
    ) -> dict[str, list[str]]:
        by_stem: dict[str, set[str]] = defaultdict(set)
        by_module: dict[str, set[str]] = defaultdict(set)

        for path in files:
            p = Path(path)
            by_stem[p.stem.lower()].add(path)
            normalized = str(p.with_suffix("")).replace("\\", "/")
            by_module[normalized.lower()].add(path)
            by_module[normalized.replace("/", ".").lower()].add(path)

        edges: dict[str, set[str]] = {path: set() for path in files}
        for source, facts in files.items():
            for token in facts.get("imports", []):
                normalized = token.lstrip("./").replace("\\", "/").lower()
                dotted = normalized.replace("/", ".")
                candidates: set[str] = set()
                for key in {
                    normalized,
                    dotted,
                    normalized.removesuffix("/index"),
                    dotted.removesuffix(".index"),
                    Path(normalized).stem,
                    normalized.split("/")[-1],
                    dotted.split(".")[-1],
                }:
                    candidates.update(by_stem.get(key, set()))
                    candidates.update(by_module.get(key, set()))

                for target in candidates:
                    if target != source:
                        edges[source].add(target)

        return {
            path: sorted(targets)
            for path, targets in edges.items()
        }

    @staticmethod
    def _pagerank(
        edges: dict[str, list[str]], iterations: int = 20
    ) -> dict[str, float]:
        nodes = list(edges)
        if not nodes:
            return {}
        damping = 0.85
        rank = {node: 1.0 / len(nodes) for node in nodes}
        incoming: dict[str, list[str]] = {node: [] for node in nodes}
        for source, targets in edges.items():
            for target in targets:
                if target in incoming:
                    incoming[target].append(source)

        for _ in range(iterations):
            next_rank: dict[str, float] = {}
            for node in nodes:
                contribution = 0.0
                for source in incoming[node]:
                    out_degree = max(1, len(edges[source]))
                    contribution += rank[source] / out_degree
                next_rank[node] = (
                    (1.0 - damping) / len(nodes)
                    + damping * contribution
                )
            rank = next_rank
        return rank

    async def ensure_index(self, repo: Path) -> IndexedRepository:
        repo_key, remote, head = await self._identity(repo)
        repo_root = self.root / repo_key
        objects_dir = repo_root / "objects"
        manifests_dir = repo_root / "manifests"
        objects_dir.mkdir(parents=True, exist_ok=True)
        manifests_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifests_dir / f"{head}.json"

        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            return IndexedRepository(
                repo_key,
                head,
                manifest_path,
                0,
                len(manifest.get("files", {})),
            )

        lock = FileLock(str(repo_root / "index.lock"), timeout=180)
        await asyncio.to_thread(lock.acquire)
        try:
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                return IndexedRepository(
                    repo_key,
                    head,
                    manifest_path,
                    0,
                    len(manifest.get("files", {})),
                )

            parsed = 0
            reused = 0
            file_objects: dict[str, str] = {}
            facts_by_path: dict[str, dict[str, Any]] = {}

            for relative in await self._tracked_files(repo):
                parsed_object = self._parse_object(repo, relative)
                if parsed_object is None:
                    continue
                digest, fresh_facts = parsed_object
                object_path = objects_dir / f"{digest}.json"
                if object_path.exists():
                    facts = {
                        **json.loads(object_path.read_text()),
                        "path": relative,
                    }
                    reused += 1
                else:
                    facts = fresh_facts
                    object_path.write_text(
                        json.dumps(fresh_facts, indent=2)
                    )
                    parsed += 1
                file_objects[relative] = digest
                facts_by_path[relative] = facts

            edges = self._resolve_dependency_edges(facts_by_path)
            ranks = self._pagerank(edges)
            manifest = {
                "repository": remote,
                "head": head,
                "files": file_objects,
                "facts": facts_by_path,
                "dependency_edges": edges,
                "ranks": ranks,
                "stats": {
                    "indexed_files": len(facts_by_path),
                    "parsed_files": parsed,
                    "reused_files": reused,
                    "symbol_count": sum(
                        len(item.get("symbols", []))
                        for item in facts_by_path.values()
                    ),
                    "dependency_edges": sum(
                        len(value) for value in edges.values()
                    ),
                },
            }
            manifest_path.write_text(json.dumps(manifest, indent=2))
            (repo_root / "latest.json").write_text(
                json.dumps(manifest, indent=2)
            )
            return IndexedRepository(
                repo_key, head, manifest_path, parsed, reused
            )
        finally:
            await asyncio.to_thread(lock.release)

    async def _manifest(self, repo: Path) -> dict[str, Any]:
        indexed = await self.ensure_index(repo)
        return json.loads(indexed.manifest_path.read_text())

    async def overview(self, repo: Path) -> dict[str, Any]:
        manifest = await self._manifest(repo)
        facts: dict[str, dict[str, Any]] = manifest["facts"]
        ranks: dict[str, float] = manifest["ranks"]

        extensions = Counter(
            Path(path).suffix.lower() or "<no-extension>"
            for path in facts
        )
        important_files = sorted(
            facts,
            key=lambda path: (-float(ranks.get(path, 0.0)), path),
        )[:40]
        repo_map = [
            {
                "path": path,
                "rank": round(float(ranks.get(path, 0.0)), 6),
                "symbols": facts[path].get("symbols", [])[:20],
            }
            for path in important_files
        ]
        important_names = {
            "README.md", "pyproject.toml", "package.json", "pom.xml",
            "build.gradle", "build.gradle.kts", "go.mod", "Cargo.toml",
            "Makefile", "docker-compose.yml", "Dockerfile",
        }
        important = [
            path for path in facts if Path(path).name in important_names
        ]
        return {
            "head": manifest["head"],
            "file_count": len(facts),
            "extensions": dict(extensions.most_common(20)),
            "important_files": important[:100],
            "stats": manifest["stats"],
            "ranked_repo_map": repo_map,
        }

    async def search(
        self, repo: Path, query: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        manifest = await self._manifest(repo)
        query_l = query.strip().lower()
        if not query_l:
            return []

        results: list[tuple[float, dict[str, Any]]] = []
        for path, facts in manifest["facts"].items():
            rank = float(manifest["ranks"].get(path, 0.0))
            score = rank * 5.0
            base_score = score
            path_l = path.lower()
            if query_l in path_l:
                score += 8.0
            if Path(path).stem.lower() == query_l:
                score += 8.0

            matched_symbols = []
            for symbol in facts.get("symbols", []):
                name_l = str(symbol["name"]).lower()
                if query_l == name_l:
                    score += 15.0
                    matched_symbols.append(symbol)
                elif query_l in name_l:
                    score += 7.0
                    matched_symbols.append(symbol)

            imports = [str(item) for item in facts.get("imports", [])]
            if any(query_l in item.lower() for item in imports):
                score += 4.0
            if query_l in str(facts.get("preview", "")).lower():
                score += 2.0

            if score > base_score:
                results.append(
                    (
                        score,
                        {
                            "path": path,
                            "score": round(score, 4),
                            "rank": round(rank, 6),
                            "symbols": matched_symbols[:10],
                            "imports": imports[:30],
                        },
                    )
                )

        results.sort(key=lambda item: (-item[0], item[1]["path"]))
        return [
            payload
            for _, payload in results[: max(1, min(limit, 50))]
        ]

    async def symbol_context(
        self, repo: Path, symbol: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        manifest = await self._manifest(repo)
        symbol_l = symbol.lower()
        reverse: dict[str, list[str]] = defaultdict(list)
        for source, targets in manifest["dependency_edges"].items():
            for target in targets:
                reverse[target].append(source)

        matches: list[dict[str, Any]] = []
        for path, facts in manifest["facts"].items():
            for item in facts.get("symbols", []):
                name = str(item["name"])
                if symbol_l == name.lower() or symbol_l in name.lower():
                    matches.append(
                        {
                            "path": path,
                            "symbol": item,
                            "imported_by": sorted(
                                reverse.get(path, [])
                            )[:20],
                            "depends_on": manifest[
                                "dependency_edges"
                            ].get(path, [])[:20],
                            "rank": round(
                                float(
                                    manifest["ranks"].get(path, 0.0)
                                ),
                                6,
                            ),
                        }
                    )
        matches.sort(
            key=lambda item: (-item["rank"], item["path"])
        )
        return matches[: max(1, min(limit, 50))]

    async def impact_analysis(
        self, repo: Path, target: str, *, depth: int = 2
    ) -> dict[str, Any]:
        manifest = await self._manifest(repo)
        facts = manifest["facts"]

        target_files = {
            path for path in facts if target.lower() in path.lower()
        }
        for path, item in facts.items():
            if any(
                target.lower() == str(symbol["name"]).lower()
                for symbol in item.get("symbols", [])
            ):
                target_files.add(path)

        reverse: dict[str, set[str]] = defaultdict(set)
        for source, targets in manifest["dependency_edges"].items():
            for dependency in targets:
                reverse[dependency].add(source)

        frontier = set(target_files)
        seen = set(target_files)
        layers: list[list[str]] = []
        for _ in range(max(0, min(depth, 5))):
            next_layer: set[str] = set()
            for current in frontier:
                next_layer.update(reverse.get(current, set()))
            next_layer -= seen
            if not next_layer:
                break
            ordered = sorted(
                next_layer,
                key=lambda path: (
                    -float(manifest["ranks"].get(path, 0.0)),
                    path,
                ),
            )
            layers.append(ordered)
            seen.update(next_layer)
            frontier = next_layer

        return {
            "target": target,
            "matched_files": sorted(target_files),
            "reverse_dependency_layers": layers,
            "impacted_files": sorted(seen - target_files),
        }

    async def dependency_hints(
        self, repo: Path, relative_path: str
    ) -> dict[str, Any]:
        manifest = await self._manifest(repo)
        facts = manifest["facts"].get(relative_path)
        if facts is None:
            raise FileNotFoundError(relative_path)
        return {
            "file": relative_path,
            "imports_and_dependency_hints": facts.get("imports", []),
            "resolved_repository_dependencies": manifest[
                "dependency_edges"
            ].get(relative_path, []),
        }

    async def stats(self, repo: Path) -> dict[str, Any]:
        manifest = await self._manifest(repo)
        return {
            "repository": manifest["repository"],
            "head": manifest["head"],
            **manifest["stats"],
        }
