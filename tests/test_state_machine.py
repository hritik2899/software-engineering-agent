import pytest

from minion.domain import TaskStatus
from minion.errors import InvalidStateTransition
from minion.state_machine import validate_transition


def test_valid_lifecycle_and_pause_resume() -> None:
    validate_transition(TaskStatus.CREATED, TaskStatus.QUEUED)
    validate_transition(TaskStatus.QUEUED, TaskStatus.PROVISIONING)
    validate_transition(TaskStatus.PROVISIONING, TaskStatus.RUNNING)
    validate_transition(TaskStatus.RUNNING, TaskStatus.WAITING_FOR_USER)
    validate_transition(TaskStatus.WAITING_FOR_USER, TaskStatus.RUNNING)
    validate_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED)


def test_terminal_state_rejects_restart() -> None:
    with pytest.raises(InvalidStateTransition):
        validate_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)


def test_failed_task_can_be_explicitly_requeued() -> None:
    validate_transition(TaskStatus.FAILED, TaskStatus.QUEUED)
