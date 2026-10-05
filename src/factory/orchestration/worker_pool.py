"""Bounded independent worker sessions over the durable task lifecycle."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from factory.domain.errors import (
    DuplicateTaskError,
    ProviderRequestError,
    TaskStateChangedError,
)
from factory.domain.models import AgentAdapter, AgentRun
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult

logger = logging.getLogger(__name__)

_RECOVERABLE_PREPARATION_ERRORS = (
    DuplicateTaskError,
    ProviderRequestError,
    TaskStateChangedError,
)


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
        wait_interval: float = 0.25,
        should_stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        on_session: Callable[[WorkerSession], None] | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 2:
            raise ValueError("max_concurrency must be 1 or 2")
        self._factory = runtime_factory
        self._max_concurrency = max_concurrency
        self._idle_interval = max(0.0, idle_interval)
        self._wait_interval = max(0.01, wait_interval)
        self._should_stop = should_stop
        self._sleep = sleep
        self._on_session = on_session

    def run_pass(self) -> tuple[WorkerSession, ...]:
        """Intake once and drain eligible sessions; isolate worker failures.

        Preparation is recoverable only for explicit concurrent-state conflicts
        that leave no worker submitted. Infrastructure, invariant and programming
        failures propagate to the CLI boundary so the watcher fails closed.
        """
        if self._should_stop():
            return ()
        try:
            coordinator = self._factory()
            coordinator.prepare_pool()
            candidates = coordinator.pool_candidates()
        except _RECOVERABLE_PREPARATION_ERRORS as exc:
            logger.error("pool pass recovered during preparation: %s", type(exc).__name__)
            return ()
        sessions: list[WorkerSession] = []
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as executor:
            active: dict[Future[RuntimeResult], _ActiveSession] = {}
            pending = iter(candidates)
            cancellation_requested = False
            while True:
                while len(active) < self._max_concurrency and not self._should_stop():
                    task_id = next(pending, None)
                    if task_id is None:
                        break
                    runtime = self._factory()
                    active[executor.submit(runtime.run_task, task_id)] = _ActiveSession(
                        task_id, runtime
                    )
                if not active:
                    break
                done, _ = wait(
                    active,
                    timeout=self._wait_interval,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    if self._should_stop() and not cancellation_requested:
                        self._cancel_active(active.values())
                        cancellation_requested = True
                    continue
                for future in done:
                    active_session = active.pop(future)
                    try:
                        session = WorkerSession(active_session.task_id, future.result())
                    except Exception as exc:  # noqa: BLE001 - one worker must not stop peers
                        logger.error(
                            "pool worker failed: task_id=%s error_type=%s",
                            active_session.task_id,
                            type(exc).__name__,
                        )
                        session = WorkerSession(
                            active_session.task_id, error_type=type(exc).__name__
                        )
                    sessions.append(session)
                    if self._on_session is not None:
                        self._on_session(session)
                if self._should_stop() and active and not cancellation_requested:
                    self._cancel_active(active.values())
                    cancellation_requested = True
        return tuple(sessions)

    def run(self) -> None:
        """Repeat passes until signalled; stop requests drain active workers."""
        while not self._should_stop():
            self.run_pass()
            if self._should_stop():
                return
            self._sleep(self._idle_interval)

    def _cancel_active(self, active: Iterable[_ActiveSession]) -> None:
        for session in tuple(active):
            try:
                run = _active_run(session.runtime, session.task_id)
                if run is None:
                    continue
                _adapter(session.runtime).cancel(run)
                logger.info("pool worker cancellation requested: task_id=%s", session.task_id)
            except Exception as exc:  # noqa: BLE001 - shutdown remains best-effort and logged
                logger.error(
                    "pool worker cancellation failed: task_id=%s error_type=%s",
                    session.task_id,
                    type(exc).__name__,
                )


@dataclass(frozen=True, slots=True)
class _ActiveSession:
    task_id: str
    runtime: FactoryRuntime


def _active_run(runtime: FactoryRuntime, task_id: str) -> AgentRun | None:
    repository = getattr(runtime, "_runs", None)
    if repository is None:
        return None
    finder = getattr(repository, "find_active_run", None)
    if not callable(finder):
        return None
    run = finder(task_id)
    return run if isinstance(run, AgentRun) else None


def _adapter(runtime: FactoryRuntime) -> AgentAdapter:
    return runtime._adapter
