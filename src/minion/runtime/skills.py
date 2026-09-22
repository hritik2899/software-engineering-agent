"""Agent Skills discovery, activation and prompt rendering.

Skills are reusable instruction bundles stored as SKILL.md files with YAML
frontmatter. Metadata is cheap to discover; full instructions are only injected when
a skill is relevant or explicitly activated.

Repository skills are instructions only. They never launch processes, import Python,
or register external tools, so opening a repository cannot execute host code.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from minion.runtime.workspace import Workspace


@dataclass(slots=True, frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    source: str
    path: Path
    markers: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    priority: int = 0

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "markers": list(self.markers),
            "keywords": list(self.keywords),
            "priority": self.priority,
        }


def _split_frontmatter(
    text: str,
) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text.strip()
    raw = text[4:end]
    body = text[end + 5 :].strip()
    metadata = yaml.safe_load(raw) or {}
    if not isinstance(metadata, dict):
        raise ValueError(
            "skill frontmatter must be a YAML mapping"
        )
    return metadata, body


class SkillManager:
    """Discover and activate skills for one task workspace."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        user_root: Path | None = None,
        max_active: int = 6,
        max_render_chars: int = 24_000,
    ):
        self.workspace = workspace
        self.user_root = user_root
        self.max_active = max_active
        self.max_render_chars = max_render_chars
        self._skills: dict[str, Skill] | None = None

    @staticmethod
    def _builtin_root() -> Path:
        return (
            Path(__file__).resolve().parents[1]
            / "builtin_skills"
        )

    @staticmethod
    def _load_skill(
        path: Path, source: str
    ) -> Skill | None:
        try:
            metadata, instructions = (
                _split_frontmatter(path.read_text())
            )
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            yaml.YAMLError,
        ):
            return None

        name = str(
            metadata.get("name")
            or path.parent.name
            or path.stem
        ).strip()
        description = str(
            metadata.get("description") or ""
        ).strip()
        if not name or not description or not instructions:
            return None

        markers = tuple(
            str(value).strip()
            for value in metadata.get("markers", [])
            if str(value).strip()
        )
        keywords = tuple(
            str(value).strip().lower()
            for value in metadata.get("keywords", [])
            if str(value).strip()
        )
        try:
            priority = int(
                metadata.get("priority", 0)
            )
        except (TypeError, ValueError):
            priority = 0

        return Skill(
            name=name,
            description=description,
            instructions=instructions,
            source=source,
            path=path,
            markers=markers,
            keywords=keywords,
            priority=priority,
        )

    def _scan_root(
        self, root: Path, source: str
    ) -> list[Skill]:
        if not root.exists():
            return []
        candidates = [
            *root.glob("*/SKILL.md"),
            *root.glob("*.md"),
        ]
        loaded: list[Skill] = []
        for path in sorted(set(candidates)):
            skill = self._load_skill(path, source)
            if skill:
                loaded.append(skill)
        return loaded

    def discover(self) -> dict[str, Skill]:
        if self._skills is not None:
            return self._skills

        ordered: list[Skill] = []
        ordered.extend(
            self._scan_root(
                self._builtin_root(),
                "builtin",
            )
        )
        if self.user_root is not None:
            ordered.extend(
                self._scan_root(
                    self.user_root.expanduser(),
                    "user",
                )
            )

        for repo_name, repo_path in sorted(
            self.workspace.repositories.items()
        ):
            ordered.extend(
                self._scan_root(
                    repo_path
                    / ".minion"
                    / "skills",
                    f"repository:{repo_name}",
                )
            )

        # Later scopes override the same skill name:
        # repository > user > built-in.
        self._skills = {
            skill.name: skill
            for skill in ordered
        }
        return self._skills

    def catalog(self) -> list[dict[str, Any]]:
        return [
            skill.catalog_entry()
            for skill in sorted(
                self.discover().values(),
                key=lambda item: (
                    -item.priority,
                    item.name,
                ),
            )
        ]

    def get(self, name: str) -> Skill | None:
        return self.discover().get(name)

    def auto_activate(
        self, instruction: str
    ) -> list[str]:
        instruction_l = instruction.lower()
        scored: list[tuple[int, str]] = []

        for skill in self.discover().values():
            score = skill.priority
            if any(
                keyword in instruction_l
                for keyword in skill.keywords
            ):
                score += 20

            marker_hits = 0
            for marker in skill.markers:
                if any(
                    (repo / marker).exists()
                    for repo
                    in self.workspace.repositories.values()
                ):
                    marker_hits += 1
            score += marker_hits * 30

            if score > skill.priority:
                scored.append((score, skill.name))

        scored.sort(
            key=lambda item: (-item[0], item[1])
        )
        return [
            name
            for _, name
            in scored[: self.max_active]
        ]

    def render(
        self, active_names: list[str]
    ) -> str:
        chunks: list[str] = []
        remaining = self.max_render_chars

        for name in active_names[: self.max_active]:
            skill = self.get(name)
            if not skill:
                continue
            header = (
                f"### Skill: {skill.name}\n"
                f"Source: {skill.source}\n"
                f"Purpose: {skill.description}\n\n"
            )
            chunk = (
                header + skill.instructions.strip()
            )
            if len(chunk) > remaining:
                chunk = (
                    chunk[: max(0, remaining)]
                    + "\n...[skill truncated]"
                )
            if chunk:
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining <= 0:
                break

        return "\n\n".join(chunks)
