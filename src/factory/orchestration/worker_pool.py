"""Bounded independent worker sessions over the durable task lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from factory.orchestration.runtime import FactoryRuntime, RuntimeResult


@dataclass(frozen=True, slots=True)
class WorkerSession:
    """One process-local slot bound to a durable task identity."""

    task_id: str
    result: RuntimeResult | None = None
    error_type: str | None = None


class WorkerPool:
    """Schedule independent tasks with one runtime instance per worker.

    Persisted tasks and runs are the source of truth after restart. A session is
    only an in-process handle; unfinished RUNNING tasks are selected first on
    the next pass and their existing agent run is resumed.
    """

    def __init__(
        self,
        runtime_factory: Callable[[], FactoryRuntime],
        *,
        max_concurrency: int = 2,
        idle_interval: float = 60.0,
        should_stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        on_session: Callable[[WorkerSession], None] | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 2:
            raise ValueError("max_concurrency must be 1 or 2")
        self._factory = runtime_factory
        self._max_concurrency = max_concurrency
        self._idle_interval = max(0.0, idle_interval)
        self._should_stop = should_stop
        self._sleep = sleep
        self._on_session = on_session

    def run_pass(self) -> tuple[WorkerSession, ...]:
        """Intake once and drain eligible sessions; isolate worker failures."""
        coordinator = self._factory()
        coordinator.prepare_pool()
        candidates = coordinator.pool_candidates()
        sessions: list[WorkerSession] = []
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as executor:
            active: dict[Future[RuntimeResult], str] = {}
            pending = iter(candidates)
            while True:
                while len(active) < self._max_concurrency and not self._should_stop():
                    task_id = next(pending, None)
                    if task_id is None:
                        break
                    active[executor.submit(self._factory().run_task, task_id)] = task_id
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    task_id = active.pop(future)
                    try:
                        session = WorkerSession(task_id, future.result())
                    except Exception as exc:  # noqa: BLE001 - one worker must not stop peers
                        session = WorkerSession(task_id, error_type=type(exc).__name__)
                    sessions.append(session)
                    if self._on_session is not None:
                        self._on_session(session)
        return tuple(sessions)

    def run(self) -> None:
        """Repeat passes until signalled; stop requests drain active workers."""
        while not self._should_stop():
            self.run_pass()
            if self._should_stop():
                return
            self._sleep(self._idle_interval)
