"""Explicit recovery of blocked tasks and failed legacy claims without dispatch."""

from __future__ import annotations

from datetime import UTC, datetime

from factory.domain.enums import RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import RetryNotAllowedError
from factory.domain.models import FactoryTask
from factory.domain.ports import RunRepository, TaskRepository
from factory.orchestration.recovery import FailureClass, RecoveryPolicy
from factory.orchestration.transitions import TaskLifecycleService


class RetryService:
    """Make one recoverable task eligible for a fresh attempt, without starting it."""

    def __init__(
        self, tasks: TaskRepository, runs: RunRepository, policy: RecoveryPolicy | None = None
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._lifecycle = TaskLifecycleService(tasks)
        self._policy = policy or RecoveryPolicy(base_backoff_seconds=0)

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
        history = self._runs.list_runs(task_id)
        latest = history[-1] if history else None
        rework_claim = self._tasks.latest_rework_feedback(task_id) is not None
        if (
            self._policy.classify_retry(task, latest, rework_claim=rework_claim)
            is FailureClass.NON_RECOVERABLE
        ):
            reason = (
                "legacy CLAIMED recovery requires latest run FAILED"
                if task.status is TaskStatus.CLAIMED
                else "latest run is not recoverable"
            )
            raise RetryNotAllowedError(f"task {task_id} {reason}")
        failed_count = sum(run.status is RunStatus.FAILED for run in history)
        gate_retry_count = sum(
            transition.from_status is TaskStatus.BLOCKED
            and transition.to_status is TaskStatus.READY
            for transition in self._tasks.history(task_id)
        )
        is_gate_retry = (
            task.status is TaskStatus.BLOCKED
            and latest is not None
            and latest.is_terminal
            and latest.validation_outcome is ValidationOutcome.GATES_FAILED
        )
        retry_count = gate_retry_count if is_gate_retry else failed_count
        if retry_count >= self._policy.run_retry_limit:
            raise RetryNotAllowedError(f"task {task_id} retry limit reached")
        if latest is not None and latest.status is RunStatus.FAILED:
            last_attempt = latest.finished_at or latest.started_at
            if (
                self._policy.base_backoff_seconds
                and last_attempt is not None
                and (datetime.now(UTC) - last_attempt).total_seconds()
                < self._policy.delay_for(failed_count)
            ):
                raise RetryNotAllowedError(f"task {task_id} retry backoff pending")
        if task.status is TaskStatus.CLAIMED:
            if not history or (
                history[-1].status is not RunStatus.FAILED
                and not (rework_claim and history[-1].is_terminal)
            ):
                raise RetryNotAllowedError(
                    f"task {task_id} legacy CLAIMED recovery requires latest run FAILED"
                )
            self._lifecycle.transition(
                task_id, TaskStatus.BLOCKED, expected_from=TaskStatus.CLAIMED
            )
        return self._lifecycle.transition(
            task_id, TaskStatus.READY, expected_from=TaskStatus.BLOCKED
        )
