"""Dispatch: turn a READY task into a durable workspace and agent run.

This is the execution seam. It coordinates the existing pieces — the lifecycle
transition service, the ``AgentAdapter`` protocol, the ``WorkspaceProvisioner``
port and the run persistence port — without knowing which engine is on the
other side or how a workspace is materialised.

The sequence is deliberate:

1. **Require READY.** A task in any other state is refused before any write.
2. **Claim atomically.** ``READY -> CLAIMED`` goes through the same
   compare-and-swap used everywhere else, so two dispatchers cannot both claim
   the same task. The loser gets :class:`DispatchConflictError`.
3. **Build a per-run workspace identity.** Branch and path derive from the
   workspace's own generated id, so a retry of the same task never reuses
   another attempt's workspace.
4. **Prepare the physical workspace.** The injected ``WorkspaceProvisioner``
   materialises the checkout. If it fails, the adapter is never called and no
   run is recorded.
5. **Start the run.** The adapter is invoked with the factory-built workspace
   and its returned run is persisted, together with the workspace, in one
   transaction.
6. **Advance to RUNNING.** After the run is durable, ``CLAIMED -> RUNNING``
   records that work has actually begun. A dispatch that fails before this point
   leaves the task ``CLAIMED`` and creates no active run, so a retry is possible
   once the blocker is removed.

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
    WorkspaceProvisioningError,
)
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    Workspace,
    new_workspace,
)
from factory.domain.ports import RunRepository, TaskRepository, WorkspaceProvisioner
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine
from factory.orchestration.transitions import TaskLifecycleService


def branch_for(task: FactoryTask, workspace: Workspace) -> str:
    """Return the isolated branch a task's workspace is created on.

    The branch is the workspace's own declared branch. It is a pure function of
    the workspace identity, so a retry that reuses a workspace is idempotent and
    a new attempt (new workspace id) gets a new branch.
    """
    del task  # Branch identity is owned by the workspace, not the task.
    return workspace.branch


class DispatchService:
    """Claims READY tasks, prepares their workspace and records the run."""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        *,
        provisioner: WorkspaceProvisioner,
        workspace_root: str = "./.workspaces",
        state_machine: TaskStateMachine | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._provisioner = provisioner
        self._lifecycle = TaskLifecycleService(tasks, state_machine)
        self._workspace_root = workspace_root.rstrip("/")

    @property
    def lifecycle(self) -> TaskLifecycleService:
        """The lifecycle service used for the claim, exposed for callers/tests."""
        return self._lifecycle

    def dispatch(self, task_id: str, adapter: AgentAdapter) -> AgentRun:
        """Dispatch ``task_id`` to ``adapter`` and return the durable run.

        Idempotency rule: a task that already has an active run (a run that is
        not in a terminal status) has already been dispatched. Such a task is in
        ``RUNNING`` or ``CLAIMED``, so the READY requirement below refuses it and
        the caller keeps the original run rather than creating a second one. The
        rule is enforced twice — by the READY check here and by the
        one-active-run unique index in storage.

        Raises:
            KeyError: if the task is unknown.
            TaskNotReadyError: if the task is not in ``READY``.
            DispatchConflictError: if another dispatcher won the claim race.
            WorkspaceProvisioningError: if the physical workspace could not be
                prepared. The adapter is not called and no run is recorded.
            AgentDispatchError: if the adapter failed to start the run. The
                failed attempt is persisted as a terminal ``FAILED`` run. The
                adapter's own exception is discarded rather than chained, so no
                engine message can leak through this error's cause or traceback.
        """
        task = self._require_task(task_id)
        if task.status is not TaskStatus.READY:
            raise TaskNotReadyError(task_id, task.status)

        claimed = self._claim(task)
        workspace = self._prepare_workspace(claimed)
        run = self._start_run(claimed, workspace, adapter)
        self._advance_to_running(claimed)
        return run

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

    def _prepare_workspace(self, task: FactoryTask) -> Workspace:
        """Build the per-run workspace identity and materialise it physically.

        Failure here is a sanitized :class:`WorkspaceProvisioningError`. The task
        stays ``CLAIMED`` — the claim already committed, and instead of rewriting
        history the factory relies on the existing ``BLOCKED``/``CANCELLED``
        paths for recovery. No run is recorded and the adapter is never invoked.
        """
        workspace = new_workspace(task, self._workspace_root)
        try:
            return self._provisioner.prepare(task, workspace)
        except WorkspaceProvisioningError:
            raise
        except Exception:  # noqa: BLE001 - normalize an unexpected provisioner error
            # A provisioner that raises something else must not leak its message
            # either: it may wrap a git or OS error that embeds a path or token.
            raise WorkspaceProvisioningError(workspace.workspace_id) from None

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

    def _advance_to_running(self, task: FactoryTask) -> None:
        """Move the claimed task to ``RUNNING`` after its run is durable.

        Ordering matters: the run is persisted first, so a task can never be
        ``RUNNING`` without a durable run behind it. ``CLAIMED -> RUNNING`` is a
        legal edge in the transition table, so this cannot fail for a task the
        claim just produced.
        """
        self._lifecycle.transition(task.task_id, TaskStatus.RUNNING)

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
