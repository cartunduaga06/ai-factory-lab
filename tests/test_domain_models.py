"""Tests for domain models and their invariants."""

from __future__ import annotations

import pytest

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RepositoryRole,
    RunStatus,
    ValidationOutcome,
)
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    QualityGate,
    QualityGateSpec,
    Repository,
    TaskSource,
    Workspace,
    new_workspace,
)


def test_repository_requires_owner_name_slug() -> None:
    repo = Repository(slug="cartunduaga06/finanza-ia", role=RepositoryRole.TARGET)
    assert repo.default_branch == "main"

    with pytest.raises(ValueError):
        Repository(slug="finanza-ia", role=RepositoryRole.TARGET)


def test_workspace_requires_isolated_branch() -> None:
    workspace = Workspace(
        repository_slug="cartunduaga06/finanza-ia", branch="task/42", path="/tmp/ws"
    )
    assert workspace.workspace_id
    assert workspace.created_at.tzinfo is not None

    with pytest.raises(ValueError):
        Workspace(repository_slug="cartunduaga06/finanza-ia", branch="")


def test_factory_task_defaults_to_discovered_from_issue() -> None:
    source = TaskSource(
        provider="github",
        repository_slug="cartunduaga06/ai-factory-lab",
        issue_number=12,
    )
    task = FactoryTask(
        title="Add retry to importer",
        target_repository="cartunduaga06/finanza-ia",
        source=source,
    )
    assert task.status.value == "DISCOVERED"
    assert task.source == source
    assert task.external_ref == "cartunduaga06/ai-factory-lab#12"
    assert task.labels == ()


def test_factory_task_without_source_has_no_external_ref() -> None:
    task = FactoryTask(title="Manual task", target_repository="cartunduaga06/finanza-ia")
    assert task.source is None
    assert task.external_ref is None


def test_factory_task_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        FactoryTask(title="   ", target_repository="cartunduaga06/finanza-ia")
    with pytest.raises(ValueError):
        FactoryTask(title="ok", target_repository="not-a-slug")


def test_agent_run_gate_aggregation() -> None:
    run = AgentRun(task_id="t1", adapter=AgentKind.OPENHANDS)
    assert run.is_terminal is False
    assert run.all_gates_passed is False

    run.gates = (
        QualityGate(name="lint", status=QualityGateStatus.PASSED),
        QualityGate(name="tests", status=QualityGateStatus.PASSED),
    )
    assert run.all_gates_passed is True

    run.status = RunStatus.FAILED
    assert run.is_terminal is True


def test_required_pending_gate_is_blocking() -> None:
    assert QualityGate(name="tests").is_blocking is True
    assert QualityGate(name="tests", status=QualityGateStatus.PASSED).is_blocking is False
    assert QualityGate(name="optional", required=False).is_blocking is False


# -- Phase 4: workspace identity and gate semantics -----------------------


def test_new_workspace_is_keyed_by_its_own_id() -> None:
    task = FactoryTask(title="Task", target_repository="cartunduaga06/finanza-ia")
    first = new_workspace(task, "/tmp/root")
    second = new_workspace(task, "/tmp/root")

    assert first.workspace_id != second.workspace_id
    for workspace in (first, second):
        assert workspace.branch == f"factory/{task.task_id}/{workspace.workspace_id}"
        assert workspace.path == f"/tmp/root/{workspace.workspace_id}"


def test_new_workspace_normalises_a_trailing_slash() -> None:
    task = FactoryTask(title="Task", target_repository="cartunduaga06/finanza-ia")
    workspace = new_workspace(task, "/tmp/root/")
    assert workspace.path == f"/tmp/root/{workspace.workspace_id}"


@pytest.mark.parametrize(
    "status",
    [QualityGateStatus.PENDING, QualityGateStatus.FAILED, QualityGateStatus.SKIPPED],
)
def test_required_non_passed_gate_is_not_green(status: QualityGateStatus) -> None:
    gate = QualityGate(name="tests", status=status)
    assert gate.is_green is False
    assert gate.is_blocking is True


def test_optional_non_passed_gate_never_blocks() -> None:
    for status in QualityGateStatus:
        gate = QualityGate(name="coverage", status=status, required=False)
        assert gate.is_blocking is False


def test_required_gates_passed_ignores_optional_failures() -> None:
    run = AgentRun(task_id="t1", adapter=AgentKind.OTHER)
    run.gates = (
        QualityGate("tests", QualityGateStatus.PASSED),
        QualityGate("coverage", QualityGateStatus.FAILED, required=False),
    )
    assert run.required_gates_passed is True
    assert run.has_required_gates is True
    assert run.all_gates_passed is False


def test_required_gates_passed_is_vacuously_true_without_required_gates() -> None:
    run = AgentRun(task_id="t1", adapter=AgentKind.OTHER)
    assert run.required_gates_passed is True
    assert run.has_required_gates is False

    run.gates = (QualityGate("coverage", QualityGateStatus.FAILED, required=False),)
    assert run.required_gates_passed is True
    assert run.has_required_gates is False


def test_validation_outcome_lifecycle() -> None:
    run = AgentRun(task_id="t1", adapter=AgentKind.OTHER)
    assert run.validation_outcome is ValidationOutcome.PENDING

    run.status = RunStatus.SUCCEEDED
    # No gates configured: nothing was defined to block the run.
    assert run.validation_outcome is ValidationOutcome.READY_FOR_NEXT_PHASE

    run.gates = (QualityGate("tests", QualityGateStatus.FAILED),)
    assert run.validation_outcome is ValidationOutcome.GATES_FAILED

    run.gates = (QualityGate("tests", QualityGateStatus.PASSED),)
    assert run.validation_outcome is ValidationOutcome.READY_FOR_NEXT_PHASE


def test_quality_gate_spec_requires_name_and_argv() -> None:
    spec = QualityGateSpec(name="tests", argv=("pytest",))
    assert spec.required is True
    assert spec.argv == ("pytest",)

    with pytest.raises(ValueError):
        QualityGateSpec(name="  ", argv=("pytest",))
    with pytest.raises(ValueError):
        QualityGateSpec(name="tests", argv=())


def test_agent_adapter_is_a_runtime_checkable_protocol() -> None:
    class DummyAdapter:
        @property
        def kind(self) -> AgentKind:
            return AgentKind.OTHER

        def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
            return AgentRun(task_id=task.task_id, adapter=self.kind, workspace=workspace)

        def collect(self, run: AgentRun) -> AgentRun:
            return run

        def cancel(self, run: AgentRun) -> None:
            return None

    assert isinstance(DummyAdapter(), AgentAdapter)
