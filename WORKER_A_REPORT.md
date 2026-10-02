# Worker A Report

## Root Cause

The watcher/pool had two shutdown robustness gaps:

- `factory pool`/`factory watch` installed cooperative SIGTERM handlers, but idle sleeps used plain `time.sleep`, so a SIGTERM during `FACTORY_WATCH_IDLE_INTERVAL` could wait for the full idle interval before exiting.
- `WorkerPool.run_pass()` waited indefinitely for active worker futures and did not request adapter cancellation on shutdown. Long-running agent sessions could therefore outlive systemd's 30 second stop window. Preparation/cycle errors before task submission also escaped the pass boundary and could terminate the watcher process.

## Branch

`hardening/worker-a-watcher-worker-a-watch-20261002T201407Z-2789209`

## Commit SHA

`PENDING_COMMIT_SHA`

## Files Changed

- `src/factory/orchestration/worker_pool.py`
- `src/factory/__main__.py`
- `tests/test_worker_pool.py`
- `docs/architecture.md`
- `docs/parallel-worker-runtime.md`
- `WORKER_A_REPORT.md`

## Tests Added

- `test_pool_continues_after_isolated_pass_preparation_error`
- `test_pool_shutdown_while_idle_returns_without_waiting`
- `test_pool_shutdown_cancels_active_cycle_and_reaps_child_process`
- `test_sigterm_stops_local_idle_pool_process_cleanly`

## Targeted Pytest Result

`.venv/bin/python -m pytest tests/test_worker_pool.py tests/test_cli.py::test_watch_cli_installs_and_restores_stop_handlers tests/test_cli.py::test_installed_stop_handler_requests_a_cooperative_stop`

Result: `12 passed in 1.19s`

## Full Pytest Result

`.venv/bin/python -m pytest`

Result: `982 passed, 1 skipped in 34.45s`

## Ruff Check Result

`.venv/bin/ruff check .`

Result: `All checks passed!`

## Ruff Format Result

`.venv/bin/ruff format --check .`

Result: `168 files already formatted`

## Mypy Result

`.venv/bin/mypy`

Result: `Success: no issues found in 103 source files`

## Security Review Result

Pass. The signal handler only sets a `threading.Event`; no complex work runs inside the handler. Pool shutdown now stops scheduling, requests cancellation through the existing engine-agnostic `AgentAdapter.cancel(run)` contract, and drains active sessions without inventing lifecycle transitions. No shell execution was added. New subprocess test code uses argv form, starts an isolated local test process group, and forcibly cleans it in test teardown if needed. Logs record task id and exception type only, avoiding raw provider messages, paths, command lines, or secrets.

## Controlled SIGTERM Test Result

`.venv/bin/python -m pytest tests/test_worker_pool.py::test_sigterm_stops_local_idle_pool_process_cleanly`

Result: `1 passed in 0.47s`

## Pending Risks

- Active shutdown still depends on the concrete adapter honoring `cancel(run)` and returning from its bounded collect/poll path. Codex is covered by cancel-marker/process-group behavior and the new no-orphan regression; remote OpenHands cancellation remains bounded by its existing HTTP timeouts and server behavior.
- Preparation errors are logged and isolated by type so provider/Trello failures do not kill the watcher. The raw exception message is intentionally not logged to avoid credential leakage.

## Worker B Boundary

Confirmed: reserved Worker B files were not modified:

- `src/factory/domain/errors.py`
- `src/factory/integrations/github/pr_state.py`
- `src/factory/orchestration/lifecycle.py`
- `src/factory/orchestration/runtime.py`

## Readiness

Ready for human review. Not pushed, not merged, not deployed.
