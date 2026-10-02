"""Durable WIP and bounded recovery decisions across restarted services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread

import pytest

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RunStatus,
    TaskStatus,
    ValidationOutcome,
)
from factory.domain.errors import RetryNotAllowedError, TaskStateChangedError
from factory.domain.models import AgentRun, FactoryTask, QualityGate
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
from factory.orchestration.reconciliation import ReconciliationService
from factory.orchestration.recovery import FailureClass, RecoveryPolicy
from factory.orchestration.retry import RetryService
from factory.orchestration.tracking import RunTrackingService
from tests.fake_adapter import FakeAgentAdapter


def test_global_claim_survives_restart_and_blocks_second_task(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(path)
    tasks.initialize()
    first = tasks.save(FactoryTask("first", "example/target", status=TaskStatus.READY))
    second = tasks.save(FactoryTask("second", "example/target", status=TaskStatus.READY))
    tasks.apply_transition(first.task_id, TaskStatus.READY, TaskStatus.CLAIMED)

    restarted = SqliteTaskRepository(path)
    with pytest.raises(TaskStateChangedError):
        restarted.apply_transition(second.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    assert restarted.get(second.task_id).status is TaskStatus.READY  # type: ignore[union-attr]
    assert restarted.history(second.task_id) == []

    restarted.apply_transition(first.task_id, TaskStatus.CLAIMED, TaskStatus.BLOCKED)
    restarted.apply_transition(second.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    assert restarted.get(second.task_id).status is TaskStatus.CLAIMED  # type: ignore[union-attr]


def test_two_worker_claims_have_one_winner(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(path)
    tasks.initialize()
    ids = [
        tasks.save(FactoryTask(str(index), "example/target", status=TaskStatus.READY)).task_id
        for index in range(2)
    ]
    barrier = Barrier(2)
    outcomes: list[str] = []

    def claim(task_id: str) -> None:
        repository = SqliteTaskRepository(path)
        barrier.wait()
        try:
            repository.apply_transition(task_id, TaskStatus.READY, TaskStatus.CLAIMED)
            outcomes.append("claimed")
        except TaskStateChangedError:
            outcomes.append("busy")

    workers = [Thread(target=claim, args=(task_id,)) for task_id in ids]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(outcomes) == ["busy", "claimed"]
    assert len(tasks.list(TaskStatus.CLAIMED)) == 1


def test_active_run_blocks_claim_even_when_task_status_diverged(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    first = tasks.save(FactoryTask("first", "example/target", status=TaskStatus.READY))
    second = tasks.save(FactoryTask("second", "example/target", status=TaskStatus.READY))
    runs.save_run(
        AgentRun(task_id=first.task_id, adapter=AgentKind.OTHER, status=RunStatus.RUNNING)
    )

    with pytest.raises(TaskStateChangedError):
        tasks.apply_transition(second.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    assert tasks.history(second.task_id) == []


def test_terminal_run_reconciliation_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("interrupted", "example/target", status=TaskStatus.READY))
    tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    tasks.apply_transition(task.task_id, TaskStatus.CLAIMED, TaskStatus.RUNNING)
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.FAILED,
            finished_at=datetime.now(UTC),
        )
    )
    adapter = FakeAgentAdapter()
    excluded = ReconciliationService(
        tasks, runs, RunTrackingService(tasks, runs), adapter, allows=lambda _: False
    )
    assert excluded.reconcile().repaired == 0
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING  # type: ignore[union-attr]
    job = ReconciliationService(tasks, runs, RunTrackingService(tasks, runs), adapter)

    assert job.reconcile().repaired == 1
    assert job.reconcile().repaired == 0
    assert tasks.get(task.task_id).status is TaskStatus.FAILED  # type: ignore[union-attr]
    assert len(tasks.history(task.task_id)) == 3
    assert [item.run_id for item in runs.list_runs(task.task_id)] == [run.run_id]
    assert adapter.dispatched == []


def test_orphan_claim_is_reported_without_duplicate_dispatch(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("interrupted", "example/target", status=TaskStatus.READY))
    tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    adapter = FakeAgentAdapter()
    job = ReconciliationService(tasks, runs, RunTrackingService(tasks, runs), adapter)

    assert job.reconcile().missing_run == 1
    assert job.reconcile().missing_run == 1
    assert tasks.get(task.task_id).status is TaskStatus.CLAIMED  # type: ignore[union-attr]
    assert runs.list_runs(task.task_id) == []
    assert adapter.dispatched == []


def test_failure_classes_and_backoff_are_bounded() -> None:
    policy = RecoveryPolicy(correction_limit=2, base_backoff_seconds=30, max_backoff_seconds=70)
    run = AgentRun(task_id="task", adapter=AgentKind.OTHER, status=RunStatus.RUNNING)
    assert policy.classify(run) is FailureClass.TRANSIENT
    run.status = RunStatus.FAILED
    assert policy.classify(run) is FailureClass.NON_RECOVERABLE
    run.status = RunStatus.SUCCEEDED
    run.gates = (QualityGate("tests", QualityGateStatus.FAILED),)
    assert policy.classify(run) is FailureClass.CORRECTABLE
    assert [policy.delay_for(index) for index in range(1, 5)] == [30, 60, 70, 70]
    with pytest.raises(ValueError):
        policy.delay_for(0)


def test_explicit_retry_obeys_persisted_backoff_and_attempt_cap(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("retry", "example/target", status=TaskStatus.BLOCKED))
    failed = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.FAILED,
            finished_at=datetime.now(UTC),
        )
    )
    policy = RecoveryPolicy(run_retry_limit=3, base_backoff_seconds=30)
    service = RetryService(tasks, runs, policy)
    with pytest.raises(RetryNotAllowedError, match="backoff"):
        service.retry(task.task_id)
    assert tasks.history(task.task_id) == []

    failed.finished_at = datetime.now(UTC) - timedelta(seconds=31)
    runs.update_run(failed)
    assert service.retry(task.task_id).status is TaskStatus.READY
    tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.BLOCKED)
    for _ in range(2):
        runs.save_run(
            AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED)
        )
    with pytest.raises(RetryNotAllowedError, match="limit"):
        service.retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED  # type: ignore[union-attr]


def test_failed_run_backoff_ignores_prior_gate_failures(tmp_path: Path) -> None:
    path = str(tmp_path / "separate-retry-counts.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("mixed failures", "example/target", status=TaskStatus.BLOCKED))
    runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            gates=(QualityGate("tests", QualityGateStatus.FAILED),),
        )
    )
    failed = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.FAILED,
            finished_at=datetime.now(UTC) - timedelta(seconds=31),
        )
    )
    # Both persisted failures consume retry budget, while only the FAILED run
    # contributes to the one-step FAILED-run backoff.
    assert (
        RetryService(tasks, runs, RecoveryPolicy(run_retry_limit=3, base_backoff_seconds=30))
        .retry(task.task_id)
        .status
        is TaskStatus.READY
    )
    assert runs.get_run(failed.run_id) is not None


def test_gate_failure_human_retry_ignores_automatic_corrections(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("gate failure", "example/target", status=TaskStatus.BLOCKED))
    for _ in range(3):
        runs.save_run(
            AgentRun(
                task_id=task.task_id,
                adapter=AgentKind.OTHER,
                status=RunStatus.SUCCEEDED,
                gates=(QualityGate("tests", QualityGateStatus.FAILED),),
            )
        )

    with pytest.raises(RetryNotAllowedError, match="limit"):
        RetryService(tasks, runs).retry(task.task_id)


def test_gate_failure_human_retries_are_bounded_by_audited_transitions(tmp_path: Path) -> None:
    path = str(tmp_path / "gate-retry-limit.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("gate retry", "example/target", status=TaskStatus.BLOCKED))
    policy = RecoveryPolicy(run_retry_limit=3, base_backoff_seconds=0)
    service = RetryService(tasks, runs, policy)

    runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            gates=(QualityGate("tests", QualityGateStatus.FAILED),),
        )
    )
    assert service.retry(task.task_id).status is TaskStatus.READY
    tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.BLOCKED)

    for _ in range(1):
        runs.save_run(
            AgentRun(
                task_id=task.task_id,
                adapter=AgentKind.OTHER,
                status=RunStatus.SUCCEEDED,
                gates=(QualityGate("tests", QualityGateStatus.FAILED),),
            )
        )
        assert service.retry(task.task_id).status is TaskStatus.READY
        tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.BLOCKED)

    runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            gates=(QualityGate("tests", QualityGateStatus.FAILED),),
        )
    )
    with pytest.raises(RetryNotAllowedError, match="limit"):
        service.retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED  # type: ignore[union-attr]


def test_explicit_retry_budget_does_not_count_healthy_successes(tmp_path: Path) -> None:
    path = str(tmp_path / "healthy-success.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("healthy success", "example/target", status=TaskStatus.BLOCKED))
    runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            gates=(QualityGate("tests", QualityGateStatus.PASSED),),
        )
    )

    with pytest.raises(RetryNotAllowedError, match="not recoverable"):
        RetryService(tasks, runs).retry(task.task_id)


@pytest.mark.parametrize(
    "latest",
    [
        AgentRun(task_id="task", adapter=AgentKind.OTHER, status=RunStatus.SUCCEEDED),
        AgentRun(task_id="task", adapter=AgentKind.OTHER, status=RunStatus.CANCELLED),
    ],
)
def test_explicit_retry_rejects_other_terminal_runs(tmp_path: Path, latest: AgentRun) -> None:
    path = str(tmp_path / f"{latest.status.value}.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(FactoryTask("not correctable", "example/target", status=TaskStatus.BLOCKED))
    latest.task_id = task.task_id
    runs.save_run(latest)

    with pytest.raises(RetryNotAllowedError, match="not recoverable"):
        RetryService(tasks, runs).retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED  # type: ignore[union-attr]
    assert tasks.history(task.task_id) == []


def test_explicit_retry_allows_durable_publication_failure_after_green_run(tmp_path: Path) -> None:
    path = str(tmp_path / "publication-retry.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(
        FactoryTask(
            "publication recovery",
            "example/target",
            status=TaskStatus.BLOCKED,
            blocked_reason="publication failed: run run-1, workspace ws-1",
        )
    )
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            gates=(QualityGate("tests", QualityGateStatus.PASSED),),
        )
    )

    assert run.validation_outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert RetryService(tasks, runs).retry(task.task_id).status is TaskStatus.READY


def test_explicit_retry_rejects_green_run_without_publication_failure_evidence(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "publication-retry-refused.db")
    tasks, runs = SqliteTaskRepository(path), SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(
        FactoryTask(
            "unrelated block",
            "example/target",
            status=TaskStatus.BLOCKED,
            blocked_reason="security review blocked publication",
        )
    )
    runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            gates=(QualityGate("tests", QualityGateStatus.PASSED),),
        )
    )

    with pytest.raises(RetryNotAllowedError, match="latest run is not recoverable"):
        RetryService(tasks, runs).retry(task.task_id)
