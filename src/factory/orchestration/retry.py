"""Explicit recovery of blocked tasks and failed legacy claims without dispatch."""

from __future__ import annotations

from factory.domain.enums import RunStatus, TaskStatus
from factory.domain.errors import RetryNotAllowedError
from factory.domain.models import FactoryTask
from factory.domain.ports import RunRepository, TaskRepository
from factory.orchestration.transitions import TaskLifecycleService


class RetryService:
    """Make one recoverable task eligible for a fresh attempt, without starting it."""

    def __init__(self, tasks: TaskRepository, runs: RunRepository) -> None:
        self._tasks = tasks
        self._runs = runs
        self._lifecycle = TaskLifecycleService(tasks)

    def retry(self, task_id: str) -> FactoryTask:
        """Make a blocked task or a failed legacy claim READY without dispatch.

        Repeated requests fail clearly; the lifecycle compare-and-swap prevents
        concurrent callers from recording duplicate retry transitions. A legacy
        claim must have a latest FAILED run and passes through BLOCKED first.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status not in {TaskStatus.BLOCKED, TaskStatus.CLAIMED}:
            raise RetryNotAllowedError(
                f"task {task_id} is not BLOCKED (status {task.status.value})"
            )
        if self._runs.find_active_run(task_id) is not None:
            raise RetryNotAllowedError(f"task {task_id} has an active run")
        if task.status is TaskStatus.CLAIMED:
            history = self._runs.list_runs(task_id)
            if not history or history[-1].status is not RunStatus.FAILED:
                raise RetryNotAllowedError(
                    f"task {task_id} legacy CLAIMED recovery requires latest run FAILED"
                )
            self._lifecycle.transition(
                task_id, TaskStatus.BLOCKED, expected_from=TaskStatus.CLAIMED
            )
        return self._lifecycle.transition(
            task_id, TaskStatus.READY, expected_from=TaskStatus.BLOCKED
        )
