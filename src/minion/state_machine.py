"""Task lifecycle validation.

Explicit state transitions make duplicate queue deliveries and races visible.
Callers should use optimistic version checks in TaskRepository as the second line
of defence.
"""
from minion.domain import TaskStatus
from minion.errors import InvalidStateTransition


_ALLOWED: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.QUEUED: {TaskStatus.PROVISIONING, TaskStatus.CANCELLING, TaskStatus.FAILED},
    TaskStatus.PROVISIONING: {TaskStatus.RUNNING, TaskStatus.CANCELLING, TaskStatus.FAILED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_FOR_USER,
        TaskStatus.CANCELLING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_FOR_USER: {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLING,
        TaskStatus.FAILED,
    },
    TaskStatus.CANCELLING: {TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.CANCELLED: set(),
    TaskStatus.FAILED: {TaskStatus.QUEUED},  # explicit retry
    TaskStatus.COMPLETED: set(),
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target == current:
        return
    if target not in _ALLOWED[current]:
        raise InvalidStateTransition(f"cannot transition task from {current} to {target}")
