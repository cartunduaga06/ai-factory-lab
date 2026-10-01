"""Bounded worker scheduling and failure isolation."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import RevisionNotPublishableError
from factory.domain.models import AgentRun, FactoryTask, PublishedRevision, Repository, TaskSource
from factory.domain.security import SecurityReview
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.security import SqliteSecurityReviewGate
from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult
from factory.orchestration.worker_pool import WorkerPool
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeWorkspaceProvisioner,
    specs,
)


class PoolRuntime:
    def __init__(self, started: threading.Barrier, *, failing: str | None = None) -> None:
        self.started = started
        self.failing = failing
        self.prepared = 0

    def prepare_pool(self) -> None:
        self.prepared += 1

    def pool_candidates(self) -> tuple[str, ...]:
        return ("task-a", "task-b")

    def run_task(self, task_id: str) -> RuntimeResult:
        self.started.wait(timeout=5)
        if task_id == self.failing:
            raise RuntimeError("worker failed")
        return RuntimeResult(
            task_id, task_id, None, None, None, None, None, "DONE", IntakeSummary()
        )


def test_two_sessions_overlap_and_failure_does_not_stop_peer() -> None:
    barrier = threading.Barrier(2)
    instances: list[PoolRuntime] = []

    def factory() -> FactoryRuntime:
        runtime = PoolRuntime(barrier, failing="task-a")
        instances.append(runtime)
        return cast("FactoryRuntime", runtime)

    sessions = WorkerPool(factory, max_concurrency=2).run_pass()
    assert {session.task_id for session in sessions} == {"task-a", "task-b"}
    assert (
        next(session for session in sessions if session.task_id == "task-a").error_type
        == "RuntimeError"
    )
    assert next(session for session in sessions if session.task_id == "task-b").result is not None
    assert len(instances) == 3  # coordinator and two independent runtimes


def test_pool_rejects_more_than_mvp_capacity() -> None:
    try:
        WorkerPool(lambda: cast("FactoryRuntime", None), max_concurrency=3)
    except ValueError:
        pass
    else:
        raise AssertionError("capacity must be bounded")


class EmptyIssueSource:
    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        del repository
        return []

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        del repository, source
        raise AssertionError("no source lookup expected")

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        del repository, source
        return True


class ConcurrentPublisher(FakeWorkspacePublisher):
    def __init__(self, barrier: threading.Barrier, failed_task_id: str) -> None:
        super().__init__()
        self._barrier = barrier
        self._failed_task_id = failed_task_id

    def publish(self, task: FactoryTask, run: AgentRun) -> PublishedRevision:
        self._barrier.wait(timeout=5)
        if task.task_id == self._failed_task_id:
            assert run.workspace is not None
            raise RevisionNotPublishableError(run.workspace.workspace_id)
        return super().publish(task, run)


class CleanSecurityInspector:
    def inspect(self, task: FactoryTask, run: AgentRun) -> SecurityReview:
        del task
        assert run.validated_revision is not None
        return SecurityReview("pool-test", run.validated_revision, ())


def test_publication_refusal_blocks_only_its_session_and_restart_skips_it(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "pool.db")
    tasks = SqliteTaskRepository(db, max_active_claims=2)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task_a = tasks.save(
        FactoryTask(
            title="Factory improvement",
            target_repository="example/factory",
            source=TaskSource("github", "example/factory", 93),
        )
    )
    task_b = tasks.save(
        FactoryTask(
            title="Product task",
            target_repository="example/product",
            source=TaskSource("github", "example/product", 94),
        )
    )
    publisher = ConcurrentPublisher(threading.Barrier(2), task_a.task_id)
    sink = FakePullRequestSink()

    def factory() -> FactoryRuntime:
        own_tasks = SqliteTaskRepository(db, max_active_claims=2)
        own_runs = SqliteRunRepository(db)
        own_prs = SqlitePullRequestRepository(db)
        return FactoryRuntime(
            intake=IssueIntakeService(EmptyIssueSource(), own_tasks),
            intake_repository=Repository("example/factory", role=RepositoryRole.CONTROL_PLANE),
            tasks=own_tasks,
            runs=own_runs,
            adapter=FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED),
            provisioner=FakeWorkspaceProvisioner(),
            workspace_root=str(tmp_path / "workspaces"),
            gate_specs=specs("tests"),
            gate_runner=FakeQualityGateRunner(),
            revision_inspector=FakeRevisionInspector(),
            publisher=publisher,
            pull_request_sink=sink,
            pull_requests=own_prs,
            base_branch="main",
            security_review=SqliteSecurityReviewGate(db, CleanSecurityInspector()),
            poll_interval=0,
            timeout=1,
            pool_mode=True,
        )

    sessions = WorkerPool(factory, max_concurrency=2).run_pass()
    assert {session.task_id for session in sessions} == {task_a.task_id, task_b.task_id}
    assert next(s for s in sessions if s.task_id == task_a.task_id).error_type == (
        "RevisionNotPublishableError"
    )
    assert next(s for s in sessions if s.task_id == task_b.task_id).result.task_status is (
        TaskStatus.WAITING_HUMAN
    )
    failed = tasks.get(task_a.task_id)
    peer = tasks.get(task_b.task_id)
    assert failed is not None and peer is not None
    assert failed.status is TaskStatus.BLOCKED
    assert peer.status is TaskStatus.WAITING_HUMAN
    run_a = runs.list_runs(task_a.task_id)[-1]
    run_b = runs.list_runs(task_b.task_id)[-1]
    assert run_a.status is RunStatus.SUCCEEDED
    assert run_a.validation_outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert run_a.workspace is not None and run_b.workspace is not None
    assert run_a.workspace.workspace_id != run_b.workspace.workspace_id
    assert run_a.workspace.branch != run_b.workspace.branch
    assert failed.blocked_reason == (
        f"publication failed: run {run_a.run_id}, workspace {run_a.workspace.workspace_id}"
    )
    assert prs.get_for_run(run_a.run_id) is None
    assert prs.get_for_run(run_b.run_id) is not None
    audit = SqliteAuditEventStore(db)
    assert any(
        event.name == "SecurityReviewPassed" and event.workspace_id == run_a.workspace.workspace_id
        for event in audit.for_task(task_a.task_id)
    )
    assert any(
        event.name == "SecurityReviewPassed" and event.workspace_id == run_b.workspace.workspace_id
        for event in audit.for_task(task_b.task_id)
    )
    assert [(edge.from_status, edge.to_status) for edge in tasks.history(task_a.task_id)][-1] == (
        TaskStatus.VALIDATING,
        TaskStatus.BLOCKED,
    )
    assert factory().pool_candidates() == ()
    assert WorkerPool(factory, max_concurrency=2).run_pass() == ()
