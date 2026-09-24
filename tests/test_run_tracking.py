"""Tests for the run-tracking / validation orchestration service.

The service is exercised with a real SQLite repository, a real
:class:`tests.fake_adapter.FakeAgentAdapter` and a real
:class:`tests.fake_workspace.FakeQualityGateRunner`. No engine, no subprocess.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from factory.domain.enums import QualityGateStatus, RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import AgentCollectError
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    QualityGate,
    QualityGateSpec,
    TaskSource,
    Workspace,
)
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
from factory.orchestration import RunTrackingService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_workspace import FakeQualityGateRunner, specs, statuses


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _tasks(db_path: str) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(db_path)
    repository.initialize()
    return repository


def _runs(db_path: str) -> SqliteRunRepository:
    repository = SqliteRunRepository(db_path)
    repository.initialize()
    return repository


def _running_task(tasks: SqliteTaskRepository) -> FactoryTask:
    task = tasks.save(
        FactoryTask(
            title="Task",
            target_repository="cartunduaga06/finanza-ia",
            source=TaskSource("github", "cartunduaga06/ai-factory-lab", 1),
        )
    )
    tasks.apply_transition(task.task_id, TaskStatus.DISCOVERED, TaskStatus.READY)
    tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    tasks.apply_transition(task.task_id, TaskStatus.CLAIMED, TaskStatus.RUNNING)
    task.status = TaskStatus.RUNNING
    return task


def _run_with_workspace(runs: SqliteRunRepository, task: FactoryTask, tmp_path: Path) -> AgentRun:
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/ws-1",
        path=str(tmp_path / "ws-1"),
    )
    Path(workspace.path).mkdir(parents=True, exist_ok=True)
    run = AgentRun(
        task_id=task.task_id,
        adapter=FakeAgentAdapter().kind,
        run_id="run-1",
        status=RunStatus.RUNNING,
        workspace=workspace,
    )
    runs.save_run(run)
    return run


def _service(
    db_path: str,
    *,
    gate_specs: tuple[QualityGateSpec, ...] = (),
    runner: FakeQualityGateRunner | None = None,
) -> RunTrackingService:
    return RunTrackingService(
        _tasks(db_path),
        _runs(db_path),
        gate_specs=gate_specs,
        gate_runner=runner,
    )


# -- non-terminal ----------------------------------------------------------


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
def test_pending_or_running_keeps_task_running(
    db_path: str, tmp_path: Path, status: RunStatus
) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    adapter = FakeAgentAdapter(collect_status=status)

    result = _service(db_path).refresh(run.run_id, adapter)

    assert result.task_status is TaskStatus.RUNNING
    assert result.run.status is status
    assert result.outcome is ValidationOutcome.PENDING
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING


# -- success / validation --------------------------------------------------


def test_succeeded_transitions_task_to_validating(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    adapter = FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)

    result = _service(db_path).refresh(run.run_id, adapter)

    assert result.run.status is RunStatus.SUCCEEDED
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    assert result.task_status is TaskStatus.VALIDATING


def test_collect_result_is_persisted(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    adapter = FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED, collect_summary="did the work")

    _service(db_path).refresh(run.run_id, adapter)

    reloaded = _runs(db_path).get_run(run.run_id)
    assert reloaded is not None
    assert reloaded.status is RunStatus.SUCCEEDED
    assert reloaded.summary == "did the work"
    assert reloaded.finished_at is not None


def test_succeeded_without_gates_reports_ready(db_path: str, tmp_path: Path) -> None:
    # Zero configured gates: documented behaviour is vacuously ready, because the
    # factory does not invent gates a repository never defined.
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)

    result = _service(db_path).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert result.run.gates == ()
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_green_required_gate_reports_ready_for_next_phase(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    runner = FakeQualityGateRunner(statuses(tests=QualityGateStatus.PASSED))

    result = _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert [g.status for g in result.run.gates] == [QualityGateStatus.PASSED]
    # It stops at VALIDATING: PR_OPEN belongs to a later phase.
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    assert runner.calls == [("tests", run.workspace.path)]  # type: ignore[union-attr]


def test_failed_required_gate_keeps_task_validating(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    runner = FakeQualityGateRunner(statuses(tests=QualityGateStatus.FAILED))

    result = _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.GATES_FAILED
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_gates_persist_durably_after_refresh(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    runner = FakeQualityGateRunner(
        statuses(tests=QualityGateStatus.PASSED, lint=QualityGateStatus.PASSED)
    )

    _service(db_path, gate_specs=specs("tests", "lint"), runner=runner).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    reopened = _runs(db_path)
    reloaded = reopened.get_run(run.run_id)
    assert reloaded is not None
    assert [g.name for g in reloaded.gates] == ["tests", "lint"]
    assert all(g.status is QualityGateStatus.PASSED for g in reloaded.gates)
    assert reloaded.status is RunStatus.SUCCEEDED
    # And the task status survived the reopen too.
    assert _tasks(db_path).get(task.task_id).status is TaskStatus.VALIDATING


def test_optional_failed_gate_does_not_block(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    runner = FakeQualityGateRunner(
        statuses(tests=QualityGateStatus.PASSED, coverage=QualityGateStatus.FAILED)
    )
    gate_specs = (*specs("tests"), QualityGateSpec(name="coverage", argv=("true",), required=False))

    result = _service(db_path, gate_specs=gate_specs, runner=runner).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE


def test_required_skipped_gate_is_not_green(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    runner = FakeQualityGateRunner(statuses(tests=QualityGateStatus.SKIPPED))

    result = _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.GATES_FAILED


def test_multiple_required_gates_require_all_to_pass(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    runner = FakeQualityGateRunner(
        statuses(tests=QualityGateStatus.PASSED, lint=QualityGateStatus.FAILED)
    )

    result = _service(db_path, gate_specs=specs("tests", "lint"), runner=runner).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.GATES_FAILED


def test_refresh_is_idempotent_for_a_terminal_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    service = _service(db_path, gate_specs=specs("tests"), runner=FakeQualityGateRunner())
    adapter = FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)

    first = service.refresh(run.run_id, adapter)
    second = service.refresh(run.run_id, adapter)

    # The second pass must not re-collect a terminal run.
    assert adapter.collected == 1
    assert second.run.gates == first.run.gates


# -- failure / cancellation ------------------------------------------------


def test_failed_transitions_task_through_legal_failure_path(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)

    result = _service(db_path).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.FAILED)
    )

    assert result.task_status is TaskStatus.FAILED
    assert tasks.get(task.task_id).status is TaskStatus.FAILED
    assert result.outcome is ValidationOutcome.PENDING


def test_cancelled_transitions_task_through_legal_cancellation_path(
    db_path: str, tmp_path: Path
) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)

    result = _service(db_path).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.CANCELLED)
    )

    assert result.task_status is TaskStatus.CANCELLED
    assert tasks.get(task.task_id).status is TaskStatus.CANCELLED


def test_unknown_run_raises_key_error(db_path: str, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        _service(db_path).refresh("missing", FakeAgentAdapter())


# -- no workspace ----------------------------------------------------------


def test_succeeded_run_without_workspace_fails_its_gates(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = AgentRun(
        task_id=task.task_id,
        adapter=FakeAgentAdapter().kind,
        run_id="run-nows",
        status=RunStatus.RUNNING,
    )
    _runs(db_path).save_run(run)

    result = _service(db_path, gate_specs=specs("tests"), runner=FakeQualityGateRunner()).refresh(
        run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    )

    assert result.outcome is ValidationOutcome.GATES_FAILED
    assert result.run.gates[0].detail == "no_workspace"


# -- crash-window reconciliation ------------------------------------------


def _persist_terminal_run(
    runs: SqliteRunRepository,
    task: FactoryTask,
    tmp_path: Path,
    *,
    status: RunStatus,
    gates: tuple = (),
) -> AgentRun:
    """Store a run that is already terminal while the task is still RUNNING.

    This is the exact state left behind by a crash between persisting the
    terminal run and applying the task transition.
    """
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/ws-1",
        path=str(tmp_path / "ws-1"),
    )
    Path(workspace.path).mkdir(parents=True, exist_ok=True)
    run = AgentRun(
        task_id=task.task_id,
        adapter=FakeAgentAdapter().kind,
        run_id="run-1",
        status=status,
        workspace=workspace,
        gates=gates,
    )
    runs.save_run(run)
    return run


def _transition_edges(tasks: SqliteTaskRepository, task_id: str) -> list[tuple]:
    return [(h.from_status, h.to_status) for h in tasks.history(task_id)]


def test_reconcile_succeeded_run_with_gates(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    gates = (QualityGate("tests", QualityGateStatus.PASSED, detail="exit_code=0"),)
    run = _persist_terminal_run(
        _runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED, gates=gates
    )
    adapter = FakeAgentAdapter()
    runner = FakeQualityGateRunner()

    result = _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(
        run.run_id, adapter
    )

    # No recollection and no gate rerun on the terminal path.
    assert adapter.collected == 0
    assert runner.calls == []
    # The task is reconciled to VALIDATING exactly once.
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    assert result.task_status is TaskStatus.VALIDATING
    edges = _transition_edges(tasks, task.task_id)
    assert edges.count((TaskStatus.RUNNING, TaskStatus.VALIDATING)) == 1
    # The run and its gates are untouched.
    reloaded = _runs(db_path).get_run(run.run_id)
    assert reloaded is not None
    assert reloaded.status is RunStatus.SUCCEEDED
    assert reloaded.gates == gates
    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE


def test_reconcile_succeeded_run_is_idempotent(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    gates = (QualityGate("tests", QualityGateStatus.PASSED, detail="exit_code=0"),)
    run = _persist_terminal_run(
        _runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED, gates=gates
    )
    adapter = FakeAgentAdapter()
    runner = FakeQualityGateRunner()
    service = _service(db_path, gate_specs=specs("tests"), runner=runner)

    service.refresh(run.run_id, adapter)
    service.refresh(run.run_id, adapter)

    assert adapter.collected == 0
    assert runner.calls == []
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    edges = _transition_edges(tasks, task.task_id)
    assert edges.count((TaskStatus.RUNNING, TaskStatus.VALIDATING)) == 1


def test_reconcile_failed_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _persist_terminal_run(_runs(db_path), task, tmp_path, status=RunStatus.FAILED)
    adapter = FakeAgentAdapter()

    result = _service(db_path).refresh(run.run_id, adapter)

    assert adapter.collected == 0
    assert tasks.get(task.task_id).status is TaskStatus.FAILED
    assert result.task_status is TaskStatus.FAILED


def test_reconcile_cancelled_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _persist_terminal_run(_runs(db_path), task, tmp_path, status=RunStatus.CANCELLED)
    adapter = FakeAgentAdapter()

    result = _service(db_path).refresh(run.run_id, adapter)

    assert adapter.collected == 0
    assert tasks.get(task.task_id).status is TaskStatus.CANCELLED
    assert result.task_status is TaskStatus.CANCELLED


def test_reconcile_already_validating_is_a_no_op(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    gates = (QualityGate("tests", QualityGateStatus.PASSED, detail="exit_code=0"),)
    run = _persist_terminal_run(
        _runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED, gates=gates
    )
    tasks.apply_transition(task.task_id, TaskStatus.RUNNING, TaskStatus.VALIDATING)
    before = _transition_edges(tasks, task.task_id)
    adapter = FakeAgentAdapter()
    runner = FakeQualityGateRunner()

    result = _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(
        run.run_id,
        adapter,
    )

    assert adapter.collected == 0
    assert runner.calls == []
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    assert _transition_edges(tasks, task.task_id) == before
    assert result.task_status is TaskStatus.VALIDATING


def test_reconcile_survives_sqlite_reopen(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    gates = (QualityGate("tests", QualityGateStatus.PASSED, detail="exit_code=0"),)
    run = _persist_terminal_run(
        _runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED, gates=gates
    )

    # Simulate a restart: build the service from freshly opened repositories.
    adapter = FakeAgentAdapter()
    runner = FakeQualityGateRunner()
    result = _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(
        run.run_id, adapter
    )

    assert adapter.collected == 0
    assert runner.calls == []
    assert result.task_status is TaskStatus.VALIDATING
    reopened_task = _tasks(db_path).get(task.task_id)
    assert reopened_task is not None
    assert reopened_task.status is TaskStatus.VALIDATING


def test_reconcile_succeeded_run_without_gates_evaluates_once(db_path: str, tmp_path: Path) -> None:
    # A SUCCEEDED run with no persisted gates while gates are configured is the
    # one recovery case: evaluate once, persist, then reconcile.
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _persist_terminal_run(_runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED)
    adapter = FakeAgentAdapter()
    runner = FakeQualityGateRunner(statuses(tests=QualityGateStatus.PASSED))
    service = _service(db_path, gate_specs=specs("tests"), runner=runner)

    result = service.refresh(run.run_id, adapter)

    assert adapter.collected == 0
    assert [g.status for g in result.run.gates] == [QualityGateStatus.PASSED]
    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    reloaded = _runs(db_path).get_run(run.run_id)
    assert reloaded is not None
    assert [g.name for g in reloaded.gates] == ["tests"]

    # A second pass must not re-run the gates that are now persisted.
    service.refresh(run.run_id, adapter)
    assert runner.calls == [("tests", run.workspace.path)]  # type: ignore[union-attr]
    assert (
        _transition_edges(tasks, task.task_id).count((TaskStatus.RUNNING, TaskStatus.VALIDATING))
        == 1
    )


def test_reconcile_succeeded_run_without_gates_and_no_specs_does_nothing(
    db_path: str, tmp_path: Path
) -> None:
    # No configured gates: the factory invents no work, it only reconciles.
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _persist_terminal_run(_runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED)
    runner = FakeQualityGateRunner()

    result = _service(db_path, gate_specs=(), runner=runner).refresh(run.run_id, FakeAgentAdapter())

    assert runner.calls == []
    assert result.run.gates == ()
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_superseded_terminal_run_does_not_drive_the_task(db_path: str, tmp_path: Path) -> None:
    # A stale terminal run from an earlier attempt must not re-apply a transition.
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    first = _persist_terminal_run(_runs(db_path), task, tmp_path, status=RunStatus.SUCCEEDED)
    # The task was retried: a newer run now owns it.
    newer = AgentRun(task_id=task.task_id, adapter=FakeAgentAdapter().kind, run_id="run-2")
    _runs(db_path).save_run(newer)

    before = _transition_edges(tasks, task.task_id)
    _service(db_path).refresh(first.run_id, FakeAgentAdapter())

    # The stale run neither changed the task nor added history.
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING
    assert _transition_edges(tasks, task.task_id) == before


# -- collect-failure security boundary -------------------------------------


def _run_snapshot(runs: SqliteRunRepository, run_id: str) -> dict:
    run = runs.get_run(run_id)
    assert run is not None
    return {
        "status": run.status,
        "summary": run.summary,
        "finished_at": run.finished_at,
        "gates": run.gates,
    }


def test_collect_failure_is_sanitized_and_leaves_no_trace(db_path: str, tmp_path: Path) -> None:
    # An engine-agnostic boundary must stay safe even when a defective adapter
    # raises a raw provider exception carrying a credential and a URL.
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    boom = RuntimeError("provider exploded with ghp_supersecret at https://credential@example.com")
    adapter = FakeAgentAdapter(collect_fail_with=boom)

    with pytest.raises(AgentCollectError) as caught:
        _service(db_path).refresh(run.run_id, adapter)

    error = caught.value
    assert error.run_id == run.run_id
    assert error.task_id == task.task_id

    formatted = "".join(traceback.format_exception(error))
    for leak in ("ghp_supersecret", "https://credential@example.com", "provider exploded"):
        assert leak not in str(error)
        assert leak not in repr(error)
        assert leak not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


def test_collect_failure_preserves_state_and_never_runs_gates(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    before = _run_snapshot(_runs(db_path), run.run_id)
    boom = RuntimeError("provider exploded with ghp_supersecret")
    runner = FakeQualityGateRunner()
    adapter = FakeAgentAdapter(collect_fail_with=boom)

    with pytest.raises(AgentCollectError):
        _service(db_path, gate_specs=specs("tests"), runner=runner).refresh(run.run_id, adapter)

    # Collection was attempted exactly once; no success was invented.
    assert adapter.collected == 1
    assert runner.calls == []
    # Task preserved, run preserved, no transition toward VALIDATING.
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING
    assert _run_snapshot(_runs(db_path), run.run_id) == before
    edges = _transition_edges(tasks, task.task_id)
    assert (TaskStatus.RUNNING, TaskStatus.VALIDATING) not in edges


def test_collect_failure_is_retryable(db_path: str, tmp_path: Path) -> None:
    # The failure is not terminal: a later refresh with a healthy adapter resumes
    # the normal lifecycle, gates included.
    tasks = _tasks(db_path)
    task = _running_task(tasks)
    run = _run_with_workspace(_runs(db_path), task, tmp_path)
    boom = RuntimeError("provider exploded with ghp_supersecret")
    service = _service(db_path, gate_specs=specs("tests"), runner=FakeQualityGateRunner())

    with pytest.raises(AgentCollectError):
        service.refresh(run.run_id, FakeAgentAdapter(collect_fail_with=boom))
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING

    healthy = FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED)
    result = service.refresh(run.run_id, healthy)

    assert healthy.collected == 1
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    assert result.task_status is TaskStatus.VALIDATING
    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    stored = _runs(db_path).get_run(run.run_id)
    assert stored is not None
    assert stored.status is RunStatus.SUCCEEDED
    assert [gate.name for gate in stored.gates] == ["tests"]
