"""Dispatch: turn a READY task into a durable workspace and agent run.

This is the Phase 2B execution seam. It coordinates the existing pieces — the
lifecycle transition service, the ``AgentAdapter`` protocol and the run
persistence port — without knowing which engine is on the other side.

The sequence is deliberate:

1. **Require READY.** A task in any other state is refused before any write.
2. **Claim atomically.** ``READY -> CLAIMED`` goes through the same
   compare-and-swap used everywhere else, so two dispatchers cannot both claim
   the same task. The loser gets :class:`DispatchConflictError`.
3. **Create the workspace and run.** The adapter is invoked with a workspace the
   factory built; its returned run is persisted, together with the workspace, in
   one transaction.

There is no engine-specific branch anywhere in this module: only
:attr:`AgentAdapter.kind` is read, and only to record which engine ran.
"""

from __future__ import annotations

from datetime import UTC, datetime

from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.errors import (
    AgentDispatchError,
    DispatchConflictError,
    DuplicateRunError,
    TaskNotReadyError,
    TaskStateChangedError,
)
from factory.domain.models import AgentAdapter, AgentRun, FactoryTask, Workspace
from factory.domain.ports import RunRepository, TaskRepository
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine
from factory.orchestration.transitions import TaskLifecycleService


def branch_for(task: FactoryTask) -> str:
    """Derive the isolated branch name a task's workspace must be created on.

    Deterministic from the task id so a retry lands on the same branch and never
    invents a second one. It is a plain string: this factory records the branch
    it intends to use but does not create it (branch creation in the target
    repository is later-phase work).
    """
    return f"factory/{task.task_id}"


class DispatchService:
    """Claims READY tasks and records the resulting workspace and run."""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        *,
        workspace_root: str = "./.workspaces",
        state_machine: TaskStateMachine | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._lifecycle = TaskLifecycleService(tasks, state_machine)
        self._workspace_root = workspace_root.rstrip("/")

    @property
    def lifecycle(self) -> TaskLifecycleService:
        """The lifecycle service used for the claim, exposed for callers/tests."""
        return self._lifecycle

    def dispatch(self, task_id: str, adapter: AgentAdapter) -> AgentRun:
        """Dispatch ``task_id`` to ``adapter`` and return the durable run.

        Idempotency rule: a task that already has an active run (a run that is
        not in a terminal status) has already been dispatched. Such a task is
        still in ``CLAIMED``, so the READY requirement below refuses it and the
        caller keeps the original run rather than creating a second one. The rule
        is enforced twice — by the READY check here and by the one-active-run
        unique index in storage.

        Raises:
            KeyError: if the task is unknown.
            TaskNotReadyError: if the task is not in ``READY``.
            DispatchConflictError: if another dispatcher won the claim race.
            AgentDispatchError: if the adapter failed to start the run. The
                failed attempt is persisted as a terminal ``FAILED`` run. The
                adapter's own exception is discarded rather than chained, so no
                engine message can leak through this error's cause or traceback.
        """
        task = self._require_task(task_id)
        if task.status is not TaskStatus.READY:
            raise TaskNotReadyError(task_id, task.status)

        claimed = self._claim(task)
        workspace = self._workspace_for(claimed)
        return self._start_run(claimed, workspace, adapter)

    # -- steps -------------------------------------------------------------

    def _require_task(self, task_id: str) -> FactoryTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _claim(self, task: FactoryTask) -> FactoryTask:
        try:
            return self._lifecycle.transition(task.task_id, TaskStatus.CLAIMED)
        except (TaskStateChangedError, InvalidTransitionError) as exc:
            # Two shapes of the same lost race. ``TaskStateChangedError`` means the
            # task was READY when this caller read it but another dispatcher
            # committed the claim first. ``InvalidTransitionError`` means this
            # caller already saw CLAIMED and the READY -> CLAIMED edge is no longer
            # legal. Either way another dispatcher won, and this caller must not
            # create a workspace or run.
            raise DispatchConflictError(task.task_id) from exc

    def _workspace_for(self, task: FactoryTask) -> Workspace:
        return Workspace(
            repository_slug=task.target_repository,
            branch=branch_for(task),
            path=f"{self._workspace_root}/{task.task_id}",
        )

    def _start_run(
        self, task: FactoryTask, workspace: Workspace, adapter: AgentAdapter
    ) -> AgentRun:
        started_at = datetime.now(UTC)
        produced: AgentRun | None
        try:
            produced = adapter.dispatch(task, workspace)
        except Exception:  # noqa: BLE001 - deliberately discarded, see below
            # The engine's exception is intentionally NOT captured. Its message
            # may embed a credential, and retaining it as ``__cause__`` or
            # ``__context__`` would expose that value to any traceback or
            # exception log. Only the fact of failure crosses this boundary; the
            # ``except`` block exits before anything is persisted or raised, so
            # the discarded exception leaves no chain behind.
            produced = None

        if produced is None:
            failed = self._record_failure(task, workspace, adapter.kind, started_at)
            raise AgentDispatchError(task.task_id, run_id=failed.run_id)

        run = AgentRun(
            task_id=task.task_id,
            adapter=adapter.kind,
            run_id=produced.run_id,
            status=produced.status,
            workspace=workspace,
            summary=produced.summary,
            started_at=produced.started_at or started_at,
            finished_at=produced.finished_at,
            gates=produced.gates,
        )
        return self._persist_run(run)

    def _record_failure(
        self,
        task: FactoryTask,
        workspace: Workspace,
        kind: AgentKind,
        started_at: datetime,
    ) -> AgentRun:
        """Persist a terminal FAILED run so a failed attempt stays auditable."""
        failed = AgentRun(
            task_id=task.task_id,
            adapter=kind,
            status=RunStatus.FAILED,
            workspace=workspace,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        return self._persist_run(failed)

    def _persist_run(self, run: AgentRun) -> AgentRun:
        try:
            return self._runs.save_run(run)
        except DuplicateRunError as exc:
            # Storage refused a second active run: another dispatcher already
            # persisted one. Surface it as the same conflict the claim race uses.
            raise DispatchConflictError(run.task_id) from exc


__all__ = ["DispatchService", "branch_for"]
