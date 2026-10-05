"""Lifecycle transitions with durable history.

This is the seam between the pure
:class:`~factory.orchestration.machine.TaskStateMachine` and persistence. The
state machine stays a pure validator; this service is what records the outcome.

The split matters: orchestration owns *whether* a transition is legal, and the
:class:`~factory.domain.ports.TaskRepository` owns the *atomic write* of the new
status plus its history row. Neither responsibility leaks into the other.
"""

from __future__ import annotations

from factory.domain.enums import TaskStatus
from factory.domain.errors import TaskStateChangedError
from factory.domain.models import FactoryTask, TaskTransition
from factory.domain.ports import TaskRepository
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine


class TaskLifecycleService:
    """Applies validated lifecycle transitions to persisted tasks."""

    def __init__(
        self,
        repository: TaskRepository,
        state_machine: TaskStateMachine | None = None,
    ) -> None:
        self._repository = repository
        self._state_machine = state_machine or TaskStateMachine()

    def transition(
        self, task_id: str, target: TaskStatus, *, expected_from: TaskStatus | None = None
    ) -> FactoryTask:
        """Move ``task_id`` to ``target``, validating and recording the change.

        Validation happens against the stored status *before* any write. An
        invalid transition raises
        :class:`~factory.orchestration.machine.InvalidTransitionError` and leaves
        both the stored status and the history untouched. ``expected_from`` can
        additionally require a specific source state before validation.

        Raises:
            KeyError: if the task is unknown.
            InvalidTransitionError: if the transition is not permitted.
            TaskStateChangedError: if the expected source no longer matches.
        """
        task = self._repository.get(task_id)
        if task is None:
            raise KeyError(task_id)

        current = task.status
        if expected_from is not None and current is not expected_from:
            raise TaskStateChangedError(task_id, expected_from, current)
        if current is target and expected_from is None:
            return task
        if not self._state_machine.can_apply(task, target):
            raise InvalidTransitionError(current, target)

        # ``current`` is the compare-and-swap guard the repository re-checks
        # inside its transaction, so a concurrent change cannot be clobbered.
        return self._repository.apply_transition(task_id, current, target)

    def history(self, task_id: str) -> list[TaskTransition]:
        """Return the recorded transition history for ``task_id``, oldest first."""
        return list(self._repository.history(task_id))

    def reconcile_terminal_resolution(self, task_id: str, expected_from: TaskStatus) -> FactoryTask:
        """Resolve a FAILED/CANCELLED task after exact provider evidence.

        This narrow exception exists for durable provider reconciliation only;
        normal lifecycle transitions continue to reject terminal states.
        """
        if expected_from not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            raise ValueError("provider reconciliation requires a terminal failure state")
        return self._repository.apply_transition(task_id, expected_from, TaskStatus.DONE)


__all__ = ["TaskLifecycleService"]
