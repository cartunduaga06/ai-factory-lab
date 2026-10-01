"""Bounded worker scheduling and failure isolation."""

from __future__ import annotations

import threading
from typing import cast

from factory.orchestration.intake import IntakeSummary
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult
from factory.orchestration.worker_pool import WorkerPool


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
