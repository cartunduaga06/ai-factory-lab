"""Idempotent reconciliation of persisted run and task lifecycle facts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from factory.domain.enums import TaskStatus
from factory.domain.models import AgentAdapter, FactoryTask
from factory.domain.ports import RunRepository, TaskRepository
from factory.orchestration.tracking import RunTrackingService


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Counts of repaired terminal states and unresolved interruptions."""

    repaired: int = 0
    missing_run: int = 0
    active_mismatch: int = 0


class ReconciliationService:
    """Refresh only durable terminal runs; never launch an agent or create a PR."""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        tracking: RunTrackingService,
        adapter: AgentAdapter,
        allows: Callable[[FactoryTask], bool] | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._tracking = tracking
        self._adapter = adapter
        self._allows = allows

    def reconcile(self) -> ReconciliationReport:
        repaired = missing = mismatch = 0
        for task in self._tasks.list():
            if self._allows is not None and not self._allows(task):
                continue
            if task.status is TaskStatus.READY:
                if self._runs.find_active_run(task.task_id) is not None:
                    mismatch += 1
                continue
            if task.status not in {
                TaskStatus.CLAIMED,
                TaskStatus.RUNNING,
                TaskStatus.VALIDATING,
                TaskStatus.PR_OPEN,
            }:
                continue
            history = self._runs.list_runs(task.task_id)
            if not history:
                missing += 1
                continue
            run = history[-1]
            if not run.is_terminal:
                if task.status is not TaskStatus.RUNNING:
                    mismatch += 1
                continue
            if run.adapter is not self._adapter.kind:
                mismatch += 1
                continue
            before = task.status
            self._tracking.refresh(run.run_id, self._adapter)
            after = self._tasks.get(task.task_id)
            if after is not None and after.status is not before:
                repaired += 1
        return ReconciliationReport(repaired, missing, mismatch)
