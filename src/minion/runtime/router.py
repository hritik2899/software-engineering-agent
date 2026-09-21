"""Task-to-agent-profile routing.

Minion does not need separate runtime implementations for bug fixing, testing and
refactoring. The router selects specialized operating guidance while all profiles
share the same CodingAgent, context manager and tool runtime.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentProfile:
    name: str
    guidance: str


GENERAL = AgentProfile(
    "general",
    "Inspect the repository, form a plan, make the smallest correct change, and verify it.",
)
BUG_FIX = AgentProfile(
    "bug_fix",
    "Reproduce or locate the failure first. Identify root cause, add regression coverage, "
    "then make the smallest fix and verify relevant tests.",
)
TESTING = AgentProfile(
    "testing",
    "Prioritize behavior discovery and meaningful automated tests. Avoid changing production "
    "behavior unless the task explicitly requires it.",
)
REFACTOR = AgentProfile(
    "refactor",
    "Preserve externally observable behavior. Establish verification before structural changes "
    "and keep the diff incremental.",
)
REVIEW = AgentProfile(
    "review",
    "Inspect existing changes and surrounding code for correctness, regressions, security, "
    "maintainability and missing tests before proposing edits.",
)


def route_agent(instruction: str) -> AgentProfile:
    lowered = instruction.lower()
    if any(word in lowered for word in ("bug", "fix", "failure", "exception", "crash")):
        return BUG_FIX
    if any(word in lowered for word in ("test", "coverage", "pytest", "unit test")):
        return TESTING
    if any(word in lowered for word in ("refactor", "cleanup", "restructure", "simplify")):
        return REFACTOR
    if any(word in lowered for word in ("review", "audit", "inspect pr")):
        return REVIEW
    return GENERAL
