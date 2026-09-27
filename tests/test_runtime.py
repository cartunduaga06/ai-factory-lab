"""Offline end-to-end tests for the one-shot runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus
from factory.domain.models import FactoryTask, Repository, TaskSource
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
    runtime = FactoryRuntime(
        intake=intake,
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "host-workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=prs,
        base_branch="main",
        poll_interval=0,
        timeout=1,
    )
    return runtime, tasks, runs, adapter, publisher, sink


def test_run_happy_path_reaches_waiting_human(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink = runtime_parts

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert tasks.list(TaskStatus.WAITING_HUMAN)
    assert len(runs.list_runs(result.task_id)) == 1  # type: ignore[arg-type]
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1


def test_retry_is_waiting_human_noop_and_does_not_duplicate_run_or_pr(runtime_parts) -> None:
    runtime, _, _, adapter, publisher, sink = runtime_parts

    first = runtime.run_once()
    second = runtime.run_once()

    assert first.run_id == second.run_id
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1
    assert second.outcome == "WAITING_HUMAN"


def test_timeout_leaves_active_run_resumable(runtime_parts) -> None:
    runtime, tasks, runs, adapter, _, _ = runtime_parts
    adapter._status = RunStatus.PENDING  # type: ignore[attr-defined]
    adapter._collect_status = RunStatus.RUNNING  # type: ignore[attr-defined]

    result = runtime.run_once()

    assert result.outcome == "TIMEOUT_RESUMABLE"
    assert tasks.list(TaskStatus.RUNNING)
    assert runs.find_active_run(result.task_id) is not None  # type: ignore[arg-type]


def test_host_path_mapping_is_explicit_and_rejects_escape(tmp_path: Path) -> None:
    mapper = WorkspacePathMapper(str(tmp_path / "host"), "/projects")
    inside = tmp_path / "host" / "ws-1"

    assert mapper.to_container(str(inside)) == "/projects/ws-1"
    with pytest.raises(WorkspacePathError):
        mapper.to_container(str(tmp_path / "elsewhere" / "ws-1"))
