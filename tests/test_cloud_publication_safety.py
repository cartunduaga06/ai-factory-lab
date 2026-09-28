"""Publication safety for Cloud runs.

A Cloud run must not reach ``WAITING_HUMAN`` — and must never open a pull request
— unless the factory could independently retrieve the exact revision Cloud
produced and validate it locally with the existing gates. These tests drive the
real ``PublicationService`` with real SQLite repositories and real port doubles,
and assert that a Cloud run without an independently validated revision fails
closed: no publish, no PR, no lifecycle advance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.errors import TaskNotPublishableError, ValidatedRevisionMissingError
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    QualityGate,
    TaskSource,
    Workspace,
)
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.orchestration import PublicationService
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _repositories(
    db_path: str,
) -> tuple[SqliteTaskRepository, SqliteRunRepository, SqlitePullRequestRepository]:
    tasks = SqliteTaskRepository(db_path)
    runs = SqliteRunRepository(db_path)
    pull_requests = SqlitePullRequestRepository(db_path)
    for repository in (tasks, runs, pull_requests):
        repository.initialize()
    return tasks, runs, pull_requests


def _task(tasks: SqliteTaskRepository) -> FactoryTask:
    task = tasks.save(
        FactoryTask(
            title="Cloud task",
            target_repository="cartunduaga06/ai-factory-lab",
            source=TaskSource("github", "cartunduaga06/ai-factory-lab", 15),
        )
    )
    for source, target in (
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.VALIDATING),
    ):
        tasks.apply_transition(task.task_id, source, target)
    task.status = TaskStatus.VALIDATING
    return task


def _cloud_run(
    runs: SqliteRunRepository,
    task: FactoryTask,
    tmp_path: Path,
    *,
    status: RunStatus = RunStatus.SUCCEEDED,
    gate_status: QualityGateStatus = QualityGateStatus.PASSED,
    validated_revision: str | None = None,
    provider_ref: str | None = "sbx:conv",
) -> AgentRun:
    """A Cloud-produced run.

    ``validated_revision`` is only ever set once the factory has materialised and
    locally validated the exact Cloud revision; until then it is ``None``.
    """
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/ws-cloud",
        path=str(tmp_path / "ws-cloud"),
    )
    Path(workspace.path).mkdir(parents=True, exist_ok=True)
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OPENHANDS,
        run_id="conv-1",
        status=status,
        workspace=workspace,
        gates=(QualityGate(name="tests", status=gate_status, required=True),),
        validated_revision=validated_revision,
        provider_ref=provider_ref,
    )
    runs.save_run(run)
    return run


def _service(
    db_path: str,
    publisher: FakeWorkspacePublisher,
    sink: FakePullRequestSink,
) -> PublicationService:
    _tasks, _runs, pull_requests = _repositories(db_path)
    return PublicationService(
        SqliteTaskRepository(db_path),
        SqliteRunRepository(db_path),
        pull_requests,
        publisher=publisher,
        sink=sink,
        base_branch="main",
        default_branch="main",
    )


def test_cloud_run_without_a_validated_revision_cannot_publish(
    db_path: str, tmp_path: Path
) -> None:
    tasks, runs, _ = _repositories(db_path)
    task = _task(tasks)
    run = _cloud_run(runs, task, tmp_path, validated_revision=None)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    with pytest.raises((ValidatedRevisionMissingError, TaskNotPublishableError)):
        _service(db_path, publisher, sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert sink.create_calls == 0
    # The task never advanced beyond VALIDATING.
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_cloud_run_that_failed_cannot_publish(db_path: str, tmp_path: Path) -> None:
    tasks, runs, _ = _repositories(db_path)
    task = _task(tasks)
    # A Cloud collect whose materialise step failed leaves the run FAILED with no
    # validated revision — exactly what the adapter produces on a missing/unsafe
    # revision identity.
    run = _cloud_run(
        runs,
        task,
        tmp_path,
        status=RunStatus.FAILED,
        validated_revision=None,
    )
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher, sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert sink.create_calls == 0


def test_cloud_run_with_a_failed_gate_cannot_publish(db_path: str, tmp_path: Path) -> None:
    tasks, runs, _ = _repositories(db_path)
    task = _task(tasks)
    run = _cloud_run(
        runs,
        task,
        tmp_path,
        gate_status=QualityGateStatus.FAILED,
        validated_revision="tree-cloud-1",
    )
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher, sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert sink.create_calls == 0


def test_cloud_run_with_an_exact_validated_revision_publishes_once(
    db_path: str, tmp_path: Path
) -> None:
    """The one publishable Cloud case: an independently validated revision, then stop."""
    tasks, runs, _ = _repositories(db_path)
    task = _task(tasks)
    run = _cloud_run(runs, task, tmp_path, validated_revision="tree-cloud-1")
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    result = _service(db_path, publisher, sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 1
    assert sink.create_calls == 1
    assert result.task_status is TaskStatus.WAITING_HUMAN
