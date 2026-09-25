"""Track an active run and validate its result.

This is the engine-agnostic seam between "an agent is working" and "the factory
has decided whether the work is acceptable". It calls only
:class:`~factory.domain.models.AgentAdapter` — never a concrete engine — and the
:class:`~factory.domain.ports.QualityGateRunner` port, never a concrete runner.

The rules implemented here are deliberately small:

* ``PENDING`` / ``RUNNING`` from the engine → the task stays ``RUNNING``.
* ``SUCCEEDED`` → the task moves ``RUNNING -> VALIDATING``, then the configured
  quality gates run in the run's workspace and their results are persisted on the
  run. The task **stops at** ``VALIDATING``: no PR exists yet, so advancing to
  ``PR_OPEN`` is explicitly out of scope.
* ``FAILED`` → the task moves through the existing legal failure edge.
* ``CANCELLED`` → the task moves through the existing legal cancellation edge.

The deterministic outcome of a validated run is :attr:`AgentRun.validation_outcome`.
A run with every required gate green reports ``READY_FOR_NEXT_PHASE`` but is not
advanced further; a run with a failed required gate reports ``GATES_FAILED`` and
also stays ``VALIDATING``. Phase 4 never dispatches a correction on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from factory.domain.enums import QualityGateStatus, RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import AgentCollectError, FactoryError, WorkspaceRevisionError
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    QualityGate,
    QualityGateSpec,
    Workspace,
)
from factory.domain.ports import (
    QualityGateRunner,
    RunRepository,
    TaskRepository,
    WorkspaceRevisionInspector,
)
from factory.orchestration.machine import InvalidTransitionError
from factory.orchestration.transitions import TaskLifecycleService

#: Name of the factory-controlled integrity gate recorded when the workspace
#: changes while the configured gates are executing. It is required, so a
#: mutation makes the run's validation outcome ``GATES_FAILED`` (which blocks
#: publication) rather than silently publishing an unverified revision.
WORKSPACE_INTEGRITY_GATE = "workspace_integrity"

#: Sanitized, non-identifying detail for the integrity gate. Never a path, a delta
#: or raw git output.
_WORKSPACE_MUTATED_DETAIL = "workspace changed during validation"


@dataclass(slots=True, frozen=True)
class RunRefresh:
    """The outcome of one refresh pass over an active run."""

    run: AgentRun
    task_status: TaskStatus
    outcome: ValidationOutcome


#: Task status each terminal run status drives the task toward. ``SUCCEEDED``
#: stops at ``VALIDATING`` on purpose; ``PR_OPEN`` belongs to a later phase.
_TERMINAL_TASK_TARGET: dict[RunStatus, TaskStatus] = {
    RunStatus.SUCCEEDED: TaskStatus.VALIDATING,
    RunStatus.FAILED: TaskStatus.FAILED,
    RunStatus.CANCELLED: TaskStatus.CANCELLED,
}


class RunTrackingService:
    """Refreshes an active run and drives the task lifecycle from its status."""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        *,
        gate_specs: Sequence[QualityGateSpec] = (),
        gate_runner: QualityGateRunner | None = None,
        revision_inspector: WorkspaceRevisionInspector | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._gate_specs = tuple(gate_specs)
        self._gate_runner = gate_runner
        self._revision_inspector = revision_inspector
        self._lifecycle = TaskLifecycleService(tasks)

    def refresh(self, run_id: str, adapter: AgentAdapter) -> RunRefresh:
        """Collect ``run_id`` from ``adapter``, persist it and advance the task.

        A run that is already terminal in storage is *not* re-collected and its
        gates are not re-run, but the task lifecycle is still reconciled: a crash
        between persisting the terminal run and applying the task transition must
        be recoverable. The terminal path is therefore idempotent — a second pass
        changes nothing.

        Raises:
            KeyError: if the run is unknown.
            AgentCollectError: if the adapter failed to collect the active run.
                The engine's own exception is discarded at this boundary rather
                than chained, so no provider message can leak through the error's
                text, cause or traceback. Nothing is persisted on this path: the
                run keeps its previous status and the task stays ``RUNNING``, so a
                later refresh retries collection.
        """
        run = self._runs.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.is_terminal:
            self._reconcile_terminal(run)
            return self._result(run)

        collection_failed = False
        try:
            adapter.collect(run)
        except Exception:  # noqa: BLE001 - deliberately discarded, see below
            # The engine's exception is intentionally NOT captured. Its message
            # may embed a credential or a provider URL, and retaining it as
            # ``__cause__`` or ``__context__`` would expose that value to any
            # traceback or exception log. Only the fact of failure crosses this
            # boundary; the ``except`` block exits before the sanitized error is
            # raised, so the discarded exception leaves no chain behind. Nothing
            # is persisted here, so the stored run and task are unchanged and the
            # attempt stays retryable.
            collection_failed = True

        if collection_failed:
            raise AgentCollectError(run.run_id, run.task_id)

        if run.status is RunStatus.SUCCEEDED:
            run.gates, run.validated_revision = self._validate_revision(run)

        self._runs.update_run(run)
        self._drive_task(run)
        return self._result(run)

    def _reconcile_terminal(self, run: AgentRun) -> None:
        """Drive the task from a run that was already terminal in storage.

        This closes the crash window: a run can be stored terminal while the
        matching task transition has not yet been applied (the process died in
        between). Re-running that reconciliation is safe — :meth:`_drive_task`
        is a no-op once the task is already in the target state.

        An already-successful run is normally trusted as-is. The one recovery
        case is a ``SUCCEEDED`` run with no persisted gates while gates are
        configured: rather than leaving the run permanently unvalidated, the
        gates are evaluated once and persisted. With no gate specs configured the
        factory invents nothing — an empty gate list is the documented result.

        A successful run with green gates but **no** bound revision (a crash
        between running the gates and persisting the revision, from a build that
        predates revision binding) is left unbound. The factory never invents a
        revision from stale gate results: an unverified revision is never claimed
        green, so such a run is simply not publishable until it is re-validated.

        Only the task's latest run may drive the task. A superseded terminal run
        (the task was retried and now has a newer run) must not rewind the
        lifecycle, so it is left alone.
        """
        if not self._is_latest_run(run):
            return
        if run.status is RunStatus.SUCCEEDED and not run.gates and self._gate_specs:
            run.gates, run.validated_revision = self._validate_revision(run)
            self._runs.update_run(run)
        self._drive_task(run)

    def _is_latest_run(self, run: AgentRun) -> bool:
        """Whether ``run`` is the most recent run for its task.

        ``list_runs`` is oldest-first, so the last entry is the newest. A run that
        is not the newest is historical: a later attempt (or a completed retry)
        owns the task now, and this run must not drive its lifecycle.
        """
        runs = self._runs.list_runs(run.task_id)
        return bool(runs) and runs[-1].run_id == run.run_id

    # -- internals ---------------------------------------------------------

    def _validate_revision(self, run: AgentRun) -> tuple[tuple[QualityGate, ...], str | None]:
        """Run the gates against the workspace and bind a validated revision.

        Returns the gate results and the durable revision identity that passed
        them (or ``None`` if no revision may be bound).

        The revision is inspected **before** and **after** the gates. Binding it
        only when the two match is what makes "only the exact revision that passed
        the gates may be published" true: a gate that mutated the workspace (or a
        concurrent writer) is detected and the run is not marked publishable. The
        gates are never silently re-run.

        * required gates failed → ``validated_revision`` is ``None``;
        * workspace changed during validation → a required
          ``workspace_integrity`` gate is recorded as failed and the revision is
          not bound;
        * no revision inspector is injected → no revision is bound. The factory
          never guesses one; publication then refuses the run.
        """
        if run.workspace is None:
            # `_evaluate_gates` records every gate as failed; nothing is bound.
            return self._evaluate_gates(run), None

        before = self._fingerprint(run.workspace)
        gates = self._evaluate_gates(run)
        after = self._fingerprint(run.workspace)

        if before is None or after is None:
            # The workspace could not be inspected, so no revision can be bound.
            return gates, None
        if before != after:
            # The workspace changed while the gates ran. Record a deterministic,
            # required integrity failure rather than trusting either snapshot, and
            # do not bind a revision. No gate is re-run.
            gates = (
                *gates,
                QualityGate(
                    name=WORKSPACE_INTEGRITY_GATE,
                    status=QualityGateStatus.FAILED,
                    detail=_WORKSPACE_MUTATED_DETAIL,
                    required=True,
                ),
            )
            return gates, None
        if not all(gate.is_green for gate in gates if gate.required):
            # A required gate is not green: nothing may be published.
            return gates, None
        return gates, after

    def _fingerprint(self, workspace: Workspace) -> str | None:
        """Return the workspace revision, or ``None`` if it cannot be inspected.

        A revision inspector is optional and an inspection failure is normalized
        to ``None`` — a failed inspection never publishes, and the sanitized
        error is discarded rather than chained.
        """
        if self._revision_inspector is None:
            return None
        try:
            return self._revision_inspector.fingerprint(workspace)
        except WorkspaceRevisionError:
            return None
        except Exception:  # noqa: BLE001 - a defective inspector must not leak
            return None

    def _evaluate_gates(self, run: AgentRun) -> tuple[QualityGate, ...]:
        """Run every configured gate in the run's workspace.

        With no gates configured the run carries no gate results and
        :attr:`AgentRun.required_gates_passed` is vacuously true, so the outcome
        is ``READY_FOR_NEXT_PHASE``. This is deliberate: the factory does not
        invent gates a repository never defined, and nothing was configured to
        block the run. The behaviour is documented rather than hidden.

        A run without a workspace cannot be validated, so its gates are recorded
        as failed — never as a silent pass.
        """
        if not self._gate_specs:
            return ()
        workspace = run.workspace
        if workspace is None or self._gate_runner is None:
            return tuple(
                QualityGate(
                    name=spec.name,
                    status=QualityGateStatus.FAILED,
                    detail="no_workspace",
                    required=spec.required,
                )
                for spec in self._gate_specs
            )
        return tuple(self._run_gate(spec, workspace) for spec in self._gate_specs)

    def _run_gate(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        runner = self._gate_runner
        assert runner is not None  # guarded by _evaluate_gates
        try:
            return runner.run(spec, workspace)
        except FactoryError:
            raise
        except Exception:  # noqa: BLE001 - normalize an unexpected runner failure
            # A runner that raises something unexpected must not leak its message
            # (it may wrap a process error carrying a command line). Only a
            # sanitized failure crosses this boundary.
            return QualityGate(
                name=spec.name,
                status=QualityGateStatus.FAILED,
                detail="runner_error",
                required=spec.required,
            )

    def _drive_task(self, run: AgentRun) -> None:
        """Apply the task transition implied by ``run.status``, if legal.

        Non-terminal engine states leave the task ``RUNNING`` untouched. A
        transition that the existing state machine does not permit is ignored
        rather than forced: the factory never rewrites the lifecycle to make an
        observed run status fit.
        """
        target = _TERMINAL_TASK_TARGET.get(run.status)
        if target is None:
            return
        task = self._tasks.get(run.task_id)
        if task is None or task.status is target:
            return
        try:
            self._lifecycle.transition(run.task_id, target)
        except InvalidTransitionError:
            # The task is not in a state that reaches ``target`` directly (for
            # example it was already recovered). Leave it alone; the recorded
            # run status remains the source of truth for what happened.
            return

    def _result(self, run: AgentRun) -> RunRefresh:
        task = self._tasks.get(run.task_id)
        task_status = task.status if task is not None else TaskStatus.DISCOVERED
        return RunRefresh(run=run, task_status=task_status, outcome=run.validation_outcome)


__all__ = ["RunRefresh", "RunTrackingService"]
