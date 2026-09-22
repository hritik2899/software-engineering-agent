"""Domain-specific exceptions.

Named exceptions keep failure boundaries explicit: authorization failures belong to
admission, invalid transitions to orchestration/idempotency, environment failures to
sandbox lifecycle, and tool failures to agent-controlled execution.
"""
class MinionError(Exception):
    """Base error for expected platform failures."""


class InvalidStateTransition(MinionError):
    pass


class EnvironmentError(MinionError):
    pass


class ToolExecutionError(MinionError):
    pass


class AuthorizationError(MinionError):
    pass
