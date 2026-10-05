# Worker A Report

## Branch

`factory-vnext/isolation-stable-runtime`

## Isolation sprint status

Implemented production-path refusal for non-production config, configurable
status port, staging process wrappers, a read-only health entrypoint, durable
nullable worker/session ownership fields for runs, and a staging-only local
soak harness. Existing SQLite active-run and active-workspace uniqueness and
the worker pool's independent exception boundary remain the enforcement base.

Staging defaults are documented in `docs/isolation-sprint.md`; wrappers pin
runtime artifacts to `/srv/ai-factory/staging/{state,workspaces,logs}` and use
port 8865. No Docker/host/production modifications were made.

## Technical debt

- The current run heartbeat is a runtime liveness hint, not an OS-process
  liveness proof. Automated orphan recovery remains explicit and fail-closed;
  a durable worker/session id improves attribution but does not prove a live
  agent process.
- Historical worktrees are intentionally not cleaned up.
- Staging credentials and provider configuration remain operator-managed in
  `/srv/ai-factory/staging/vnext.env`; no secret is stored in the repository.

## Isolation sprint validation

- Branch: `factory-vnext/isolation-stable-runtime`
- Staging health smoke: `FACTORY_PYTHON=/tmp/pr54-qa-venv/bin/python
  ops/staging/health` — passed, status `IDLE`, database at
  `/srv/ai-factory/staging/state/factory.db`.
- Full pytest: **1,054 passed, 1 skipped**.
- `ruff check .`: passed.
- `ruff format --check .`: passed (182 files).
- `mypy`: passed (110 source files).
- `git diff --check`: passed.
- Existing concurrent runtime test demonstrates distinct active workspaces and
  one failed task `BLOCKED` while its unrelated peer reaches `WAITING_HUMAN`.
  `ops/harness/soak` captures this local fake-provider suite for repeat runs.
- `ops/release/prepare-rc` is the clean-tree check and exact-SHA RC record
  command. RC verdict is **READY FOR PRODUCTION PROMOTION — HUMAN GATE REQUIRED**
  after it succeeds on the committed SHA. No merge or deployment is authorized
  or performed by this worker.

## Remaining blockers and evidence limits

- No live-provider or Finanza IA E2E was run; it is explicitly deferred. The
  staging-only E2E commands and evidence checklist are in
  `docs/isolation-sprint.md`.
- No production promotion, merge or deployment was performed. Human approval
  remains required.
- Run heartbeat is not proof of worker process liveness. Ownership metadata is
  for attribution; orphan recovery still requires the separate explicit
  operator-authorized fail-closed procedure.
- Exact implementation and report commit SHAs are recorded in the final
  response. The RC command emits the candidate SHA and gate summary.

## Intermittent SIGTERM Root Cause

The intermittent >5s shutdown was a signal-handler deadlock/restart race in the
idle path. The old SIGTERM handler called `threading.Event.set()` while the main
thread could be interrupted inside `threading.Event.wait()`. In the reproduced
hang, the handler tried to reacquire the event condition lock already held by the
interrupted wait. A first pipe-based attempt still saw rare delay because Python
can restart `select()` before the Python-level handler writes to the pipe.

## SIGTERM Correction

`factory pool`/`factory watch` now use `_CooperativeStop`: a plain stop flag plus
a nonblocking pipe registered with `signal.set_wakeup_fd()`. CPython writes to
the pipe from the low-level signal path, waking idle `select()` deterministically
before the Python handler runs. The CLI restores both previous signal handlers
and the previous wakeup fd on exit.

Graceful shutdown is preserved: no product SIGKILL path was added, active cycles
still request adapter cancellation and drain through the existing worker-pool
logic, and no lifecycle transition or persistence path was changed.

## Controlled SIGTERM Evidence

Before the fix, the idle SIGTERM reproduction timed out 8/50 times with a max of
about 5.009s and fast cases around 30ms.

After the fix, 50 consecutive controlled idle SIGTERM iterations completed:

- min: `0.028083s`
- max: `0.038502s`
- approximate mean: `0.030114s`
- failures: `0`

## Exception-Boundary Design

`WorkerPool.run_pass()` no longer catches `Exception` around
`runtime_factory()`, `prepare_pool()` and `pool_candidates()`.

Recoverable preparation exceptions:

- `DuplicateTaskError`
- `TaskStateChangedError`

Those are explicit concurrent-state conflicts before any worker future is
submitted, so the affected scheduling pass is abandoned with sanitized
exception-type evidence and the watcher may continue.

Fatal behavior:

- runtime construction errors, configuration errors, infrastructure/storage
  errors, provider errors, invariant violations and programming errors propagate
  to the CLI boundary;
- the CLI prints only the sanitized exception type and exits fail-closed;
- task-bound worker future isolation remains unchanged after a durable task id
  has been assigned.

## Files Modified

- `WORKER_A_REPORT.md`
- `docs/architecture.md`
- `src/factory/__main__.py`
- `src/factory/orchestration/worker_pool.py`
- `tests/test_cli.py`
- `tests/test_worker_pool.py`

## Tests Added/Changed

- Strengthened `test_sigterm_stops_local_idle_pool_process_cleanly` to run 20
  consecutive SIGTERM subprocess iterations and assert sub-second shutdown.
- Changed preparation-boundary coverage to
  `test_pool_continues_after_recoverable_pass_preparation_error`.
- Added `test_pool_preparation_programming_error_fails_closed`.
- Updated CLI signal-handler tests for wakeup-fd restoration.

## Validation

- Targeted regression tests:
  `.venv/bin/python -m pytest tests/test_worker_pool.py::test_sigterm_stops_local_idle_pool_process_cleanly tests/test_worker_pool.py::test_pool_continues_after_recoverable_pass_preparation_error tests/test_worker_pool.py::test_pool_preparation_programming_error_fails_closed tests/test_cli.py::test_installed_stop_handler_requests_a_cooperative_stop tests/test_cli.py::test_watch_cli_installs_and_restores_stop_handlers`
  Result: `5 passed in 4.16s`
- Full pytest: `.venv/bin/python -m pytest`
  Result: `983 passed, 1 skipped in 37.32s`
- Ruff check: `.venv/bin/python -m ruff check .`
  Result: `All checks passed!`
- Ruff format: `.venv/bin/python -m ruff format --check .`
  Result: `169 files already formatted`
- Mypy: `.venv/bin/python -m mypy`
  Result: `Success: no issues found in 103 source files`

## Security Review

Pass. The signal handler path no longer re-enters a `threading.Event` lock. The
wakeup pipe uses nonblocking file descriptors, restores the process-global
wakeup fd, and does not run shell commands or expose secrets. Logs still include
only task ids and exception type names. No merge, deployment, host, Docker,
SQLite-manual, product-repository or GitHub-settings behavior was added.

## Orphan-Process Verification

After the 50-iteration SIGTERM run, a focused process check found no remaining
test children matching `_CooperativeStop`, `idle_interval=30.0`,
`test_sigterm_stops_local_idle_pool_process_cleanly` or `time.sleep(30)`.

## Reserved-File Verification

Confirmed by `git diff --name-only`: no reserved Worker B files were modified.

Reserved files not touched:

- `src/factory/domain/errors.py`
- `src/factory/integrations/github/pr_state.py`
- `src/factory/orchestration/lifecycle.py`
- `src/factory/orchestration/runtime.py`

## Pending Risks

- Active shutdown still depends on the concrete adapter honoring `cancel(run)`
  and returning from its bounded collect/poll path.
- The final commit SHA is self-referential and cannot be embedded in this file
  before the commit exists; the final response reports the immutable SHA.
