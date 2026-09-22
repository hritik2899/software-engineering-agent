"""Deterministic safety policy for agent-controlled shell commands.

The LLM can propose arbitrary shell text, but it does not get final authority over
execution. This module is the last deterministic gate before a command reaches a
workspace. It is defense in depth on top of sandbox resource/capability isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


class CommandPolicy:
    """Validate shell commands using small, explainable deny rules."""

    _DENY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"(^|[;&|]\s*)sudo(?:\s|$)"),
            "privilege escalation is blocked",
        ),
        (
            re.compile(r"(^|[;&|]\s*)su(?:\s|$)"),
            "privilege escalation is blocked",
        ),
        (
            re.compile(r"\brm\s+-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$|\*)"),
            "recursive deletion of filesystem root is blocked",
        ),
        (
            re.compile(r"\b(?:mkfs|fdisk|parted)\b"),
            "disk/filesystem mutation is blocked",
        ),
        (
            re.compile(r"\bdd\s+[^;\n]*(?:of=/dev/|if=/dev/)"),
            "raw block-device access is blocked",
        ),
        (
            re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b"),
            "host lifecycle commands are blocked",
        ),
        (
            re.compile(r"\bmount\b|\bumount\b"),
            "mount namespace mutation is blocked",
        ),
        (
            re.compile(r"\bgit\s+push\b[^\n]*(?:--force|-f)(?:\s|$)"),
            "force-pushing is blocked",
        ),
        (
            re.compile(r"\bgit\s+reset\s+--hard\b"),
            "destructive git reset is blocked",
        ),
        (
            re.compile(r"(?:>|>>)\s*/(?:etc|proc|sys|boot|dev)/"),
            "writes to system paths are blocked",
        ),
        (
            re.compile(r"\bchmod\s+(?:-R\s+)?777\s+/"),
            "global permission changes are blocked",
        ),
    )

    def __init__(self, mode: str = "enforce"):
        if mode not in {"enforce", "audit"}:
            raise ValueError(
                "command policy mode must be 'enforce' or 'audit'"
            )
        self.mode = mode

    def evaluate(self, command: str) -> PolicyDecision:
        normalized = command.strip()
        if not normalized:
            return PolicyDecision(False, "empty command")

        for pattern, reason in self._DENY_PATTERNS:
            if pattern.search(normalized):
                if self.mode == "audit":
                    return PolicyDecision(
                        True, f"audit-only warning: {reason}"
                    )
                return PolicyDecision(False, reason)

        return PolicyDecision(True)
