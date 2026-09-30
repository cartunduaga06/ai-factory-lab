"""Tests for the automatic worker loop.

Two levels are covered:

* The loop itself, against a scripted runtime, so idle/sleep/stop/bounds and the
  "one invocation per iteration" (WIP=1) contract are deterministic.
* The loop against the **real** :class:`FactoryRuntime` with the existing
  non-mock doubles, so sequential processing and idempotent re-invocation are
  proven on real orchestration rather than a mock.
"""

from __future__ import annotations

from pathlib import Path

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus
from factory.domain.models import FactoryTask, Repository, TaskSource
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult
from factory.orchestration.watch import FactoryWatcher
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeSecurityReviewGate,
    FakeWorkspaceProvisioner,
    specs,
)


def _result(outcome: str, task_status: TaskStatus | None = None) -> RuntimeResult:
    return RuntimeResult(None, None, task_status, None, None, None, None, outcome, IntakeSummary())


class ScriptedRuntime:
    """A runtime double that returns a scripted outcome per call and counts calls."""

    def __init__(self, outcomes: list[str]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def run_once(self) -> RuntimeResult:
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return _result(outcome)


def test_idle_iteration_waits_then_retries() -> None:
    runtime = ScriptedRuntime(["NO_ELIGIBLE_TASK", "NO_ELIGIBLE_TASK", "WAITING_HUMAN"])
    sleeps: list[float] = []
    watcher = FactoryWatcher(
        runtime=runtime,  # type: ignore[arg-type]
        idle_interval=7.5,
        max_iterations=3,
        sleep=sleeps.append,
    )

    outcome = watcher.run()

    assert runtime.calls == 3
    assert sleeps == [7.5, 7.5]  # a wait only when there was no work
    assert outcome.iterations == 3
    assert outcome.idle_waits == 2
    assert outcome.processed == 1
    assert outcome.stopped is True


def test_waiting_human_stops_after_one_iteration_without_waiting() -> None:
    runtime = ScriptedRuntime(["WAITING_HUMAN", "WAITING_HUMAN"])
    sleeps: list[float] = []
    watcher = FactoryWatcher(
        runtime=runtime,  # type: ignore[arg-type]
        idle_interval=30.0,
        sleep=sleeps.append,
    )

    outcome = watcher.run()

    assert runtime.calls == 1
    assert sleeps == []
    assert outcome.iterations == 1
    assert outcome.processed == 1
    assert outcome.idle_waits == 0
    assert outcome.stopped is True


def test_waiting_human_task_status_stops_even_with_different_outcome() -> None:
    class StatusRuntime:
        calls = 0

        def run_once(self) -> RuntimeResult:
            self.calls += 1
            return _result("PUBLISHED", TaskStatus.WAITING_HUMAN)

    runtime = StatusRuntime()
    outcome = FactoryWatcher(runtime=runtime, idle_interval=0.0).run()  # type: ignore[arg-type]

    assert runtime.calls == 1
    assert outcome.stopped is True


def test_stop_request_ends_loop_between_iterations() -> None:
    runtime = ScriptedRuntime(["TIMEOUT_RESUMABLE", "TIMEOUT_RESUMABLE"])
    stop_after_one = {"calls": 0}

    def should_stop() -> bool:
        stop_after_one["calls"] += 1
        # First call happens before iteration 1; the second call (before
        # iteration 2) reports a pending stop signal.
        return stop_after_one["calls"] > 1

    watcher = FactoryWatcher(
        runtime=runtime,  # type: ignore[arg-type]
        idle_interval=0.0,
        should_stop=should_stop,
    )

    outcome = watcher.run()

    assert runtime.calls == 1
    assert outcome.stopped is True
    assert outcome.iterations == 1


def test_already_stopped_runs_no_iteration() -> None:
    runtime = ScriptedRuntime(["TIMEOUT_RESUMABLE"])
    watcher = FactoryWatcher(
        runtime=runtime,  # type: ignore[arg-type]
        idle_interval=0.0,
        should_stop=lambda: True,
    )

    outcome = watcher.run()

    assert runtime.calls == 0
    assert outcome.iterations == 0
    assert outcome.stopped is True


def test_max_iterations_bounds_loop() -> None:
    runtime = ScriptedRuntime(["TIMEOUT_RESUMABLE"])
    watcher = FactoryWatcher(
        runtime=runtime,  # type: ignore[arg-type]
        idle_interval=0.0,
        max_iterations=3,
    )

    outcome = watcher.run()

    assert runtime.calls == 3
    assert outcome.iterations == 3
    assert outcome.stopped is False


def _runtime_with(
    root: Path, task_specs: list[tuple[str, int]]
) -> tuple[FactoryRuntime, SqliteRunRepository, FakeAgentAdapter, FakePullRequestSink]:
    """Build a real runtime over the given ``(title, issue)`` tasks."""
    db = str(root / "factory.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    # Persist the tasks through real intake so selection sees DISCOVERED work.
    created = [
        tasks.save(
            FactoryTask(
                title=title,
                target_repository="example/target",
                source=TaskSource("github", "example/control", issue),
            )
        )
        for title, issue in task_specs
    ]

    class StaticSource:
        def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
            del repository
            return list(created)

        def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
            del repository
            return next(t for t in created if t.source == source)

        def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
            del repository
            return any(t.source == source for t in created)

    adapter = FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    sink = FakePullRequestSink()
    runtime = FactoryRuntime(
        security_review=FakeSecurityReviewGate(),
        intake=IssueIntakeService(StaticSource(), tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(root / "host-workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=FakeWorkspacePublisher(),
        pull_request_sink=sink,
        pull_requests=prs,
        base_branch="main",
        poll_interval=0,
        timeout=1,
    )
    return runtime, runs, adapter, sink


def test_watch_keeps_wip_at_one_while_a_task_awaits_human(tmp_path: Path) -> None:
    # Recovery states take precedence in the reusable runtime, so while a task is
    # WAITING_HUMAN it remains the single in-flight task (WIP=1) and a second
    # eligible issue is not started until a human advances the first.
    runtime, runs, adapter, sink = _runtime_with(tmp_path, [("first", 1), ("second", 2)])

    outcome = FactoryWatcher(runtime=runtime, idle_interval=0.0, max_iterations=3).run()

    assert outcome.iterations == 1
    assert outcome.idle_waits == 0
    assert outcome.stopped is True
    assert len(adapter.dispatched) == 1
    assert len(runs.list_runs()) == 1
    assert sink.create_calls == 1
    assert outcome.last_result is not None
    assert outcome.last_result.task_status is TaskStatus.WAITING_HUMAN


def test_watch_does_not_duplicate_work_already_waiting_human(tmp_path: Path) -> None:
    runtime, runs, adapter, sink = _runtime_with(tmp_path, [("only", 1)])

    # A fresh watch invocation re-observes the same task without duplicating work.
    first = FactoryWatcher(runtime=runtime, idle_interval=0.0).run()
    outcome = FactoryWatcher(runtime=runtime, idle_interval=0.0).run()

    assert first.iterations == outcome.iterations == 1
    assert outcome.stopped is True
    assert len(adapter.dispatched) == 1
    assert len(runs.list_runs()) == 1
    assert sink.create_calls == 1
    assert outcome.last_result is not None
    assert outcome.last_result.task_status is TaskStatus.WAITING_HUMAN


def test_watch_processes_the_next_task_after_a_human_advances_the_first(
    tmp_path: Path,
) -> None:
    runtime, runs, adapter, sink = _runtime_with(tmp_path, [("first", 1), ("second", 2)])

    # Iteration 1 handles the first task up to WAITING_HUMAN.
    FactoryWatcher(runtime=runtime, idle_interval=0.0, max_iterations=1).run()
    first_task_id = runs.list_runs()[0].task_id
    assert len(adapter.dispatched) == 1

    # A human reviews and completes the first task (the only non-automatic step).
    runtime._dispatch.lifecycle.transition(first_task_id, TaskStatus.DONE)  # type: ignore[attr-defined]

    # The next iteration now picks up the second, still-eligible task.
    FactoryWatcher(runtime=runtime, idle_interval=0.0, max_iterations=1).run()

    assert len(adapter.dispatched) == 2
    assert len(runs.list_runs()) == 2
    assert sink.create_calls == 2
    assert {run.task_id for run in runs.list_runs()} != {first_task_id}
