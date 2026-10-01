"""Durable pool capacity and isolation across lifecycle and restart."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus
from factory.domain.errors import TaskStateChangedError
from factory.domain.models import AgentRun, FactoryTask, Repository, TaskSource, new_workspace
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime
from factory.orchestration.transitions import TaskLifecycleService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeSecurityReviewGate,
    FakeWorkspaceProvisioner,
    specs,
)
from tests.test_runtime import FakeIssueSource


def test_two_claims_run_independently_and_restart_preserves_isolation(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(path, max_active_claims=2)
    runs = SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    first = tasks.save(
        FactoryTask(
            "factory improvement",
            "example/factory",
            status=TaskStatus.READY,
            source=TaskSource("github", "example/factory", 86),
        )
    )
    second = tasks.save(
        FactoryTask(
            "product improvement",
            "example/dulces",
            status=TaskStatus.READY,
            source=TaskSource("github", "example/dulces", 12),
        )
    )
    third = tasks.save(FactoryTask("queued", "example/third", status=TaskStatus.READY))
    barrier = Barrier(2)

    def start(task: FactoryTask) -> AgentRun:
        local_tasks = SqliteTaskRepository(path, max_active_claims=2)
        local_runs = SqliteRunRepository(path)
        lifecycle = TaskLifecycleService(local_tasks)
        barrier.wait(timeout=5)
        lifecycle.transition(task.task_id, TaskStatus.CLAIMED)
        workspace = new_workspace(task, str(tmp_path / "workspaces"))
        run = local_runs.save_run(
            AgentRun(
                task_id=task.task_id,
                adapter=AgentKind.OTHER,
                status=RunStatus.RUNNING,
                workspace=workspace,
                project_id=task.project_id,
            )
        )
        lifecycle.transition(task.task_id, TaskStatus.RUNNING)
        return run

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start, task) for task in (first, second)]
        active = [future.result(timeout=10) for future in futures]

    with pytest.raises(TaskStateChangedError):
        TaskLifecycleService(tasks).transition(third.task_id, TaskStatus.CLAIMED)
    assert tasks.get(third.task_id).status is TaskStatus.READY  # type: ignore[union-attr]
    assert tasks.history(third.task_id) == []

    restarted_tasks = SqliteTaskRepository(path, max_active_claims=2)
    restarted_runs = SqliteRunRepository(path)
    assert {task.task_id for task in restarted_tasks.list(TaskStatus.RUNNING)} == {
        first.task_id,
        second.task_id,
    }
    assert {run.run_id for run in restarted_runs.list_runs() if not run.is_terminal} == {
        run.run_id for run in active
    }
    assert len({run.workspace.path for run in active if run.workspace}) == 2
    assert len({run.workspace.branch for run in active if run.workspace}) == 2
    assert {run.workspace.repository_slug for run in active if run.workspace} == {
        "example/factory",
        "example/dulces",
    }

    prs = SqlitePullRequestRepository(path)
    prs.initialize()
    for task, original in zip((first, second), active, strict=True):
        resumed = FactoryRuntime(
            intake=IssueIntakeService(FakeIssueSource(task), restarted_tasks),
            intake_repository=Repository(task.target_repository, role=RepositoryRole.TARGET),
            tasks=restarted_tasks,
            runs=restarted_runs,
            adapter=FakeAgentAdapter(kind=AgentKind.OTHER),
            provisioner=FakeWorkspaceProvisioner(),
            workspace_root=str(tmp_path / "workspaces"),
            gate_specs=specs("tests"),
            gate_runner=FakeQualityGateRunner(),
            revision_inspector=FakeRevisionInspector(),
            publisher=FakeWorkspacePublisher(),
            pull_request_sink=FakePullRequestSink(),
            pull_requests=prs,
            base_branch="main",
            security_review=FakeSecurityReviewGate(),
            timeout=0,
            poll_interval=0,
            pool_mode=True,
        ).run_task(task.task_id)
        assert resumed.run_id == original.run_id
        assert resumed.outcome == "TIMEOUT_RESUMABLE"
        assert len(restarted_runs.list_runs(task.task_id)) == 1

    lifecycle = TaskLifecycleService(restarted_tasks)
    lifecycle.transition(first.task_id, TaskStatus.CANCELLED)
    active[0].status = RunStatus.CANCELLED
    active[0].finished_at = datetime.now(UTC)
    restarted_runs.update_run(active[0])
    assert restarted_tasks.get(second.task_id).status is TaskStatus.RUNNING  # type: ignore[union-attr]
    assert restarted_runs.find_active_run(second.task_id).run_id == active[1].run_id  # type: ignore[union-attr]
    lifecycle.transition(third.task_id, TaskStatus.CLAIMED)


def test_serial_claim_guard_stays_global(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    serial = SqliteTaskRepository(path)
    serial.initialize()
    first = serial.save(FactoryTask("first", "example/one", status=TaskStatus.READY))
    second = serial.save(FactoryTask("second", "example/two", status=TaskStatus.READY))
    TaskLifecycleService(serial).transition(first.task_id, TaskStatus.CLAIMED)
    with pytest.raises(TaskStateChangedError):
        TaskLifecycleService(SqliteTaskRepository(path)).transition(
            second.task_id, TaskStatus.CLAIMED
        )
