"""Minimal enforcement of the task lifecycle.

Deliberately not a workflow engine: it validates transitions and nothing more.
Actual scheduling, dispatch and persistence arrive in later phases.
"""

from __future__ import annotations

from datetime import UTC, datetime

from factory.domain.enums import TaskStatus
from factory.domain.errors import InvalidTransitionError
from factory.domain.models import FactoryTask
from factory.orchestration.lifecycle import TERMINAL_STATES, TRANSITIONS, can_transition


class TaskStateMachine:
    """Applies validated lifecycle transitions to a :class:`FactoryTask`.

    Holds no state of its own — the task is the state. This makes the machine
    trivially safe to share and easy to replace with a persisted variant later.
    """

__all__ = ["InvalidTransitionError", "TaskStateMachine"]


    def can_apply(self, task: FactoryTask, target: TaskStatus) -> bool:
        return can_transition(task.status, target)

    def apply(self, task: FactoryTask, target: TaskStatus) -> FactoryTask:
        """Move ``task`` to ``target``, mutating and returning it.

        Raises:
            InvalidTransitionError: if the transition is not permitted.
        """
        if not self.can_apply(task, target):
            raise InvalidTransitionError(task.status, target)
        task.status = target
        task.updated_at = datetime.now(UTC)
        return task

    @staticmethod
    def is_terminal(status: TaskStatus) -> bool:
        return status in TERMINAL_STATES

    @staticmethod
    def legal_targets(status: TaskStatus) -> frozenset[TaskStatus]:
        return TRANSITIONS[status]
