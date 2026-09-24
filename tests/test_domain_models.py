"""Tests for domain models and their invariants."""

from __future__ import annotations

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RepositoryRole, RunStatus
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    QualityGate,
    Repository,
    TaskSource,
    Workspace,
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
