"""Offline end-to-end tests for the one-shot runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RepositoryRole,
    RunStatus,
    TaskStatus,
)
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    QualityGate,
    Repository,
    TaskSource,
    Workspace,
)
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.integrations.openhands import WorkspacePathError, WorkspacePathMapper
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeWorkspaceProvisioner,
    specs,
)


class FakeIssueSource:
    def __init__(self, task: FactoryTask) -> None:
        self.task = task

    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        del repository
        return [self.task]

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        del repository, source
        return self.task


@pytest.fixture
def runtime_parts(tmp_path: Path):
    db = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = FactoryTask(
        title="Issue 8 acceptance",
        body="exercise the factory",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 8),
    )
    intake = IssueIntakeService(FakeIssueSource(task), tasks)
    adapter = FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()
    gate_runner = FakeQualityGateRunner()
    runtime = FactoryRuntime(
        intake=intake,
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "host-workspaces"),
        gate_specs=specs("tests"),
        gate_runner=gate_runner,
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=prs,
        base_branch="main",
        poll_interval=0,
        timeout=1,
    )
    return runtime, tasks, runs, adapter, publisher, sink, gate_runner


def test_run_happy_path_reaches_waiting_human(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert tasks.list(TaskStatus.WAITING_HUMAN)
    assert len(runs.list_runs(result.task_id)) == 1  # type: ignore[arg-type]
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1


def test_retry_is_waiting_human_noop_and_does_not_duplicate_run_or_pr(runtime_parts) -> None:
    runtime, _, _, adapter, publisher, sink, _ = runtime_parts

    first = runtime.run_once()
    second = runtime.run_once()

    assert first.run_id == second.run_id
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1
    assert second.outcome == "WAITING_HUMAN"


def test_timeout_leaves_active_run_resumable(runtime_parts) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    adapter._status = RunStatus.PENDING  # type: ignore[attr-defined]
    adapter._collect_status = RunStatus.RUNNING  # type: ignore[attr-defined]

    result = runtime.run_once()

    assert result.outcome == "TIMEOUT_RESUMABLE"
    assert tasks.list(TaskStatus.RUNNING)
    assert runs.find_active_run(result.task_id) is not None  # type: ignore[arg-type]


def test_active_run_resume_collects_same_run_without_dispatch(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    adapter._status = RunStatus.PENDING  # type: ignore[attr-defined]
    adapter._collect_status = RunStatus.RUNNING  # type: ignore[attr-defined]
    runtime._timeout = 0  # type: ignore[attr-defined]

    first = runtime.run_once()
    adapter._collect_status = RunStatus.SUCCEEDED  # type: ignore[attr-defined]
    second = runtime.run_once()

    assert first.outcome == "TIMEOUT_RESUMABLE"
    assert second.outcome == "WAITING_HUMAN"
    assert second.run_id == first.run_id
    assert len(adapter.dispatched) == 1
    assert len(runs.list_runs(first.task_id)) == 1  # type: ignore[arg-type]
    assert tasks.get(first.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[arg-type]
    assert publisher.calls == 1
    assert sink.create_calls == 1


def test_validated_run_resume_skips_agent_and_continues_to_publication(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    runtime._intake.intake(runtime._intake_repository)  # type: ignore[attr-defined]
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    for target in (TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.VALIDATING):
        runtime._dispatch.lifecycle.transition(task.task_id, target)  # type: ignore[attr-defined]
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/validated",
        path=str(Path(runtime._dispatch._workspace_root) / "validated"),  # type: ignore[attr-defined]
    )
    Path(workspace.path).mkdir(parents=True, exist_ok=True)
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        status=RunStatus.SUCCEEDED,
        workspace=workspace,
        gates=(QualityGate("tests", QualityGateStatus.PASSED, required=True),),
        validated_revision="tree-validated",
    )
    runs.save_run(run)

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert result.run_id == run.run_id
    assert adapter.dispatched == []
    assert publisher.calls == 1
    assert sink.create_calls == 1


def test_failed_required_gate_does_not_publish(runtime_parts) -> None:
    runtime, tasks, _, _, publisher, sink, gate_runner = runtime_parts
    gate_runner._statuses["tests"] = QualityGateStatus.FAILED  # type: ignore[attr-defined]

    result = runtime.run_once()

    assert result.outcome == "QUALITY_GATES_FAILED"
    assert result.task_status is TaskStatus.VALIDATING
    assert publisher.calls == 0
    assert sink.create_calls == 0


def test_host_path_mapping_is_explicit_and_rejects_escape(tmp_path: Path) -> None:
    mapper = WorkspacePathMapper(str(tmp_path / "host"), "/projects")
    inside = tmp_path / "host" / "ws-1"

    assert mapper.to_container(str(inside)) == "/projects/ws-1"
    with pytest.raises(WorkspacePathError):
        mapper.to_container(str(tmp_path / "elsewhere" / "ws-1"))


@pytest.mark.parametrize("failed", [False, True])
def test_legacy_validating_runtime_revalidates_without_agent_or_new_records(
    runtime_parts, failed: bool
) -> None:
    runtime, tasks, runs, adapter, publisher, sink, runner = runtime_parts
    runtime._intake.intake(runtime._intake_repository)
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    for target in (TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.VALIDATING):
        runtime._dispatch.lifecycle.transition(task.task_id, target)
    root = Path(runtime._dispatch._workspace_root)
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/legacy",
        path=str(root / "legacy"),
    )
    Path(workspace.path).mkdir(parents=True)
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            workspace=workspace,
            gates=(QualityGate("tests", QualityGateStatus.PASSED),),
        )
    )
    if failed:
        runner._statuses["tests"] = QualityGateStatus.FAILED

    result = runtime.run_once()
    stored = runs.get_run(run.run_id)
    assert stored is not None
    assert result.run_id == run.run_id
    assert runner.calls == [("tests", workspace.path)]
    assert adapter.dispatched == [] and adapter.collected == 0
    assert len(runs.list_runs(task.task_id)) == 1
    assert stored.workspace == workspace
    assert runs.get_workspace(workspace.workspace_id) == workspace
    assert list(root.iterdir()) == [Path(workspace.path)]
    assert (stored.validated_revision is not None) == (not failed)
    assert publisher.calls == sink.create_calls == (0 if failed else 1)
    assert result.outcome == ("QUALITY_GATES_FAILED" if failed else "WAITING_HUMAN")
    assert result.task_status is (TaskStatus.VALIDATING if failed else TaskStatus.WAITING_HUMAN)
    runtime.run_once()
    assert runner.calls == [("tests", workspace.path)]
    assert adapter.dispatched == [] and adapter.collected == 0
