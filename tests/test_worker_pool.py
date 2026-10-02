"""Bounded worker scheduling and failure isolation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import cast

import pytest

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import RetryNotAllowedError, RevisionNotPublishableError
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PublishedRevision,
    PullRequest,
    Repository,
    TaskSource,
)
from factory.domain.security import SecurityReview
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.security import SqliteSecurityReviewGate
from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.retry import RetryService
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


def test_pool_continues_after_isolated_pass_preparation_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    prepared = 0
    sleeps = 0

    class FlakyCoordinator:
        def prepare_pool(self) -> None:
            nonlocal prepared
            prepared += 1
            if prepared == 1:
                raise RuntimeError("provider failed")

        def pool_candidates(self) -> tuple[str, ...]:
            return ()

    def sleep(seconds: float) -> None:
        nonlocal sleeps
        assert seconds == 0.0
        sleeps += 1
        if sleeps == 2:
            stop.set()

    with caplog.at_level("ERROR"):
        WorkerPool(
            lambda: cast("FactoryRuntime", FlakyCoordinator()),
            idle_interval=0.0,
            should_stop=stop.is_set,
            sleep=sleep,
        ).run()

    assert prepared == 2
    assert "pool pass failed during preparation: RuntimeError" in caplog.text


def test_pool_shutdown_while_idle_returns_without_waiting() -> None:
    stop = threading.Event()
    sleeps: list[float] = []
    prepared = 0

    class EmptyCoordinator:
        def prepare_pool(self) -> None:
            nonlocal prepared
            prepared += 1

        def pool_candidates(self) -> tuple[str, ...]:
            return ()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        stop.set()

    WorkerPool(
        lambda: cast("FactoryRuntime", EmptyCoordinator()),
        idle_interval=30.0,
        should_stop=stop.is_set,
        sleep=sleep,
    ).run()

    assert prepared == 1
    assert sleeps == [30.0]


class ActiveRunRepository:
    def __init__(self, run: AgentRun) -> None:
        self._run = run

    def find_active_run(self, task_id: str) -> AgentRun | None:
        return self._run if task_id == self._run.task_id else None


class ProcessKillingAdapter:
    def __init__(self, child: subprocess.Popen[bytes]) -> None:
        self._child = child
        self.cancelled = threading.Event()

    @property
    def kind(self) -> AgentKind:
        return AgentKind.OTHER

    def cancel(self, run: AgentRun) -> None:
        del run
        self.cancelled.set()
        if self._child.poll() is None:
            os.killpg(self._child.pid, signal.SIGTERM)


def test_pool_shutdown_cancels_active_cycle_and_reaps_child_process(tmp_path: Path) -> None:
    stop = threading.Event()
    started = threading.Event()
    task_id = "task-active"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    run = AgentRun(task_id=task_id, adapter=AgentKind.OTHER, status=RunStatus.RUNNING)
    adapter = ProcessKillingAdapter(child)

    class Coordinator:
        def prepare_pool(self) -> None:
            return None

        def pool_candidates(self) -> tuple[str, ...]:
            return (task_id,)

    class ActiveRuntime:
        _runs = ActiveRunRepository(run)
        _adapter = adapter

        def run_task(self, task_id: str) -> RuntimeResult:
            started.set()
            assert child.wait(timeout=5) is not None
            return RuntimeResult(
                task_id, run.run_id, None, None, None, None, None, "CANCELLED", IntakeSummary()
            )

    def request_stop() -> None:
        assert started.wait(timeout=2)
        stop.set()

    stopper = threading.Thread(target=request_stop)
    runtimes = [Coordinator(), ActiveRuntime()]

    def factory() -> FactoryRuntime:
        return cast("FactoryRuntime", runtimes.pop(0))

    stopper.start()
    try:
        sessions = WorkerPool(
            factory,
            max_concurrency=1,
            wait_interval=0.01,
            should_stop=stop.is_set,
        ).run_pass()
    finally:
        stopper.join(timeout=2)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)

    assert adapter.cancelled.is_set()
    assert child.poll() is not None
    assert len(sessions) == 1
    assert sessions[0].task_id == task_id
    assert sessions[0].result is not None


def test_sigterm_stops_local_idle_pool_process_cleanly() -> None:
    script = textwrap.dedent(
        """
        import threading

        from factory.__main__ import _install_stop_handlers, _stop_aware_sleep
        from factory.orchestration.worker_pool import WorkerPool

        class Runtime:
            def prepare_pool(self):
                return None

            def pool_candidates(self):
                return ()

        stop = threading.Event()
        _install_stop_handlers(stop)
        print("ready", flush=True)
        WorkerPool(
            lambda: Runtime(),
            idle_interval=30.0,
            should_stop=stop.is_set,
            sleep=_stop_aware_sleep(stop),
        ).run()
        print("stopped", flush=True)
        """
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    for _ in range(2):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)

        assert process.returncode == 0, stderr
        assert "stopped" in stdout


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


class InterruptedSink(FakePullRequestSink):
    """Simulate an uncertain provider response after it created the PR."""

    def open_pull_request(self, pull_request: PullRequest) -> PullRequest:
        opened = super().open_pull_request(pull_request)
        raise RuntimeError(f"response lost for PR {opened.number}")


@pytest.mark.parametrize("failure", ["before_push", "before_pr", "after_pr"])
def test_publication_retry_resumes_same_run_after_restart(tmp_path: Path, failure: str) -> None:
    db = str(tmp_path / "retry-pool.db")
    tasks, runs, prs = (
        SqliteTaskRepository(db),
        SqliteRunRepository(db),
        SqlitePullRequestRepository(db),
    )
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = tasks.save(FactoryTask("publication recovery", "example/factory"))
    adapter = FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    publisher = FakeWorkspacePublisher(
        fail_with=RuntimeError("push failed") if failure == "before_push" else None
    )
    sink = (
        InterruptedSink()
        if failure == "after_pr"
        else FakePullRequestSink(fail_create=failure == "before_pr")
    )

    def factory() -> FactoryRuntime:
        own_tasks, own_runs, own_prs = (
            SqliteTaskRepository(db),
            SqliteRunRepository(db),
            SqlitePullRequestRepository(db),
        )
        return FactoryRuntime(
            intake=IssueIntakeService(EmptyIssueSource(), own_tasks),
            intake_repository=Repository("example/factory", role=RepositoryRole.CONTROL_PLANE),
            tasks=own_tasks,
            runs=own_runs,
            pull_requests=own_prs,
            adapter=adapter,
            provisioner=FakeWorkspaceProvisioner(),
            workspace_root=str(tmp_path / "workspaces"),
            gate_specs=specs("tests"),
            gate_runner=FakeQualityGateRunner(),
            revision_inspector=FakeRevisionInspector(),
            publisher=publisher,
            pull_request_sink=sink,
            base_branch="main",
            security_review=SqliteSecurityReviewGate(db, CleanSecurityInspector()),
            poll_interval=0,
            timeout=1,
            pool_mode=True,
        )

    assert WorkerPool(factory).run_pass()[0].error_type is not None
    original = runs.list_runs(task.task_id)[0]
    assert original.workspace is not None
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED
    assert factory().pool_candidates() == ()
    publisher._fail_with = None
    sink._fail_create = False
    assert RetryService(tasks, runs).retry(task.task_id).status is TaskStatus.VALIDATING
    # Repeated authorization cannot record another transition.
    with pytest.raises(RetryNotAllowedError):
        RetryService(tasks, runs).retry(task.task_id)
    recovered = WorkerPool(factory).run_pass()[0]
    assert recovered.error_type is None
    assert recovered.result is not None
    assert recovered.result.task_status is TaskStatus.WAITING_HUMAN
    assert runs.list_runs(task.task_id) == [original]
    assert adapter.dispatched == [(task.task_id, original.workspace.workspace_id)]
    pr = prs.get_for_run(original.run_id)
    assert pr is not None and pr.head_branch == original.workspace.branch
    assert pr.commit_sha is not None
    assert sink.create_calls == (2 if failure == "before_pr" else 1)
    assert len(sink._open) == 1
    assert (TaskStatus.BLOCKED, TaskStatus.VALIDATING) in [
        (edge.from_status, edge.to_status) for edge in tasks.history(task.task_id)
    ]
