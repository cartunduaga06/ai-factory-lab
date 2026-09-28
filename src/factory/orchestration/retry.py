"""Explicit recovery of blocked tasks without dispatch or history mutation."""

from __future__ import annotations

from factory.domain.enums import TaskStatus
from factory.domain.errors import RetryNotAllowedError
from factory.domain.models import FactoryTask
from factory.domain.ports import RunRepository, TaskRepository
from factory.orchestration.transitions import TaskLifecycleService


class RetryService:
    """Make one blocked task eligible for a fresh attempt, without starting it."""

    def __init__(self, tasks: TaskRepository, runs: RunRepository) -> None:
        self._tasks = tasks
        self._runs = runs
        self._lifecycle = TaskLifecycleService(tasks)

    def retry(self, task_id: str) -> FactoryTask:
        """Apply BLOCKED -> READY only when no active run exists.

        Repeated requests fail clearly; the lifecycle compare-and-swap prevents
        concurrent callers from recording duplicate retry transitions.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is not TaskStatus.BLOCKED:
            raise RetryNotAllowedError(
                f"task {task_id} is not BLOCKED (status {task.status.value})"
            )
        if self._runs.find_active_run(task_id) is not None:
            raise RetryNotAllowedError(f"task {task_id} has an active run")
        return self._lifecycle.transition(
            task_id, TaskStatus.READY, expected_from=TaskStatus.BLOCKED
        )
