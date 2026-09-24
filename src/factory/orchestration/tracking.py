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
from factory.domain.errors import FactoryError
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    QualityGate,
    QualityGateSpec,
    Workspace,
)
from factory.domain.ports import QualityGateRunner, RunRepository, TaskRepository
from factory.orchestration.machine import InvalidTransitionError
from factory.orchestration.transitions import TaskLifecycleService


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
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._gate_specs = tuple(gate_specs)
        self._gate_runner = gate_runner
        self._lifecycle = TaskLifecycleService(tasks)

    def refresh(self, run_id: str, adapter: AgentAdapter) -> RunRefresh:
        """Collect ``run_id`` from ``adapter``, persist it and advance the task.

        A run that is already terminal in storage is returned unchanged: it has
        already been collected, its gates have already been evaluated, and
        re-running them would be neither idempotent nor cheap.

        Raises:
            KeyError: if the run is unknown.
        """
        run = self._runs.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.is_terminal:
            return self._result(run)

        adapter.collect(run)
        if run.status is RunStatus.SUCCEEDED:
            run.gates = self._evaluate_gates(run)

        self._runs.update_run(run)
        self._drive_task(run)
        return self._result(run)

    # -- internals ---------------------------------------------------------

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
