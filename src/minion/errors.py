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
