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

    def transition(self, task_id: str, target: TaskStatus) -> FactoryTask:
        """Move ``task_id`` to ``target``, validating and recording the change.

        Validation happens against the stored status *before* any write. An
        invalid transition raises
        :class:`~factory.orchestration.machine.InvalidTransitionError` and leaves
        both the stored status and the history untouched.

        Raises:
            KeyError: if the task is unknown.
            InvalidTransitionError: if the transition is not permitted.
        """
        task = self._repository.get(task_id)
        if task is None:
            raise KeyError(task_id)

        current = task.status
        if not self._state_machine.can_apply(task, target):
            raise InvalidTransitionError(current, target)

        # ``current`` is the compare-and-swap guard the repository re-checks
        # inside its transaction, so a concurrent change cannot be clobbered.
        return self._repository.apply_transition(task_id, current, target)

    def history(self, task_id: str) -> list[TaskTransition]:
        """Return the recorded transition history for ``task_id``, oldest first."""
        return list(self._repository.history(task_id))


__all__ = ["TaskLifecycleService"]
