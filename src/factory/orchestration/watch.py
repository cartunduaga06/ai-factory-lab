"""Automatic worker loop over the existing one-shot runtime.

The factory already exposes every phase as ``FactoryRuntime.run_once``. This
module adds no pipeline of its own: it calls that method in sequence, one task at
a time (WIP=1), and waits between iterations when there is nothing eligible.

Boundaries are deliberate:

* Exactly one runtime invocation may be in flight. ``run_once`` already selects
  and completes at most one task, and ``FactoryRuntime`` reconciles by persisted
  state, so restarting or re-invoking cannot duplicate a task, run, branch or
  Pull Request.
* The loop never merges, deploys or mutates an Issue. A task that reaches
  ``WAITING_HUMAN`` is handed back to a human and the watcher exits.
* Stopping is cooperative. A stop request is observed after the current
  invocation returns, so a signal never interrupts a half-finished task or
  corrupts persisted state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from factory.domain.enums import TaskStatus
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult

# No eligible work means the worker waits before checking again.
IDLE_OUTCOMES = frozenset({"NO_ELIGIBLE_TASK", "WIP_BUSY", "BACKOFF_PENDING"})


@dataclass(slots=True, frozen=True)
class WatchOutcome:
    """Sanitized summary of a bounded watch run.

    Carries only counters and the last runtime result (already secret-free), so
    it is safe to log or return from the CLI. ``processed`` counts non-idle
    iterations (a task was advanced or reconciled); ``idle_waits`` counts the
    iterations that found no eligible task. ``stopped`` means a stop request or
    a human gate ended the loop.
    """

    iterations: int
    processed: int
    idle_waits: int
    stopped: bool
    last_result: RuntimeResult | None


class FactoryWatcher:
    """Run ``FactoryRuntime.run_once`` sequentially until told to stop.

    ``max_iterations`` bounds the loop for tests and for an operator who wants a
    finite number of passes; ``None`` means run until a stop signal or human gate. The
    ``sleep`` and ``should_stop`` seams are injected so the loop is deterministic
    under test and never depends on wall-clock time.
    """

    def __init__(
        self,
        *,
        runtime: FactoryRuntime,
        idle_interval: float,
        max_iterations: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._runtime = runtime
        self._idle_interval = max(0.0, idle_interval)
        self._max_iterations = max_iterations
        self._sleep = sleep
        self._should_stop = should_stop

    def run(self) -> WatchOutcome:
        """Drive sequential iterations until a stop request, human gate, or bound."""
        iterations = 0
        processed = 0
        idle_waits = 0
        stopped = False
        last_result: RuntimeResult | None = None

        while self._max_iterations is None or iterations < self._max_iterations:
            if self._should_stop():
                stopped = True
                break

            result = self._runtime.run_once()
            last_result = result
            iterations += 1
            if result.outcome in IDLE_OUTCOMES:
                idle_waits += 1
            else:
                processed += 1
            if (
                result.outcome in {"WAITING_HUMAN", "SPRINT_PAUSED"}
                or result.task_status is TaskStatus.WAITING_HUMAN
            ):
                stopped = True
                break
            if result.outcome in IDLE_OUTCOMES | {"TIMEOUT_RESUMABLE"}:
                self._sleep(self._idle_interval)

        return WatchOutcome(
            iterations=iterations,
            processed=processed,
            idle_waits=idle_waits,
            stopped=stopped,
            last_result=last_result,
        )


__all__ = ["IDLE_OUTCOMES", "FactoryWatcher", "WatchOutcome"]
