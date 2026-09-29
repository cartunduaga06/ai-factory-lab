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
  ``WAITING_HUMAN`` is handed back to a human and is not re-dispatched — the
  runtime reconciles it idempotently rather than opening a second PR.
* Stopping is cooperative. A stop request is observed after the current
  invocation returns, so a signal never interrupts a half-finished task or
  corrupts persisted state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from factory.orchestration.runtime import FactoryRuntime, RuntimeResult

# Outcomes that mean no eligible work was available for this iteration. The
# worker waits before checking again; every other outcome made progress and an
# immediate next iteration is safe.
IDLE_OUTCOMES = frozenset({"NO_ELIGIBLE_TASK"})


@dataclass(slots=True, frozen=True)
class WatchOutcome:
    """Sanitized summary of a bounded watch run.

    Carries only counters and the last runtime result (already secret-free), so
    it is safe to log or return from the CLI. ``processed`` counts non-idle
    iterations (a task was advanced or reconciled); ``idle_waits`` counts the
    iterations that found no eligible task.
    """

    iterations: int
    processed: int
    idle_waits: int
    stopped: bool
    last_result: RuntimeResult | None


class FactoryWatcher:
    """Run ``FactoryRuntime.run_once`` sequentially until told to stop.

    ``max_iterations`` bounds the loop for tests and for an operator who wants a
    finite number of passes; ``None`` means run until a stop signal arrives. The
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
        """Drive sequential iterations until a stop signal or the bound is hit."""
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
                self._sleep(self._idle_interval)
            else:
                processed += 1

        return WatchOutcome(
            iterations=iterations,
            processed=processed,
            idle_waits=idle_waits,
            stopped=stopped,
            last_result=last_result,
        )


__all__ = ["IDLE_OUTCOMES", "FactoryWatcher", "WatchOutcome"]
