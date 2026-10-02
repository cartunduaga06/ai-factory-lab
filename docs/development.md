# Development

How to work in `ai-factory-lab`. Conventions and hard rules also live in
[`../AGENTS.md`](../AGENTS.md); this document covers the day-to-day workflow.

## Requirements

- Python 3.11 or newer (3.13 is what the baseline is verified on).
- `git` and, for GitHub operations, `gh`.

## Setup

```bash
git clone https://github.com/cartunduaga06/ai-factory-lab.git
cd ai-factory-lab

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"
```

`-e ".[dev]"` installs the package in editable mode plus pytest, Ruff and mypy.

## Configuration

Copy the example and fill in only what you need:

```bash
cp .env.example .env
```

Every integration is optional. With nothing set, defaults are safe: no
credential, `FACTORY_ENV=development`, SQLite for the database URL and
`./.workspaces` for the workspace root.

Verify the resolved configuration without exposing secrets:

```bash
python -m factory --show-config
```

The output masks all credentials (`"***"`). Never commit `.env`.

## Commands

| Purpose | Command |
|---|---|
| Tests | `pytest` |
| Lint | `ruff check .` |
| Format check | `ruff format --check .` |
| Apply formatting | `ruff format .` |
| Types | `mypy` |
| Config sanity check | `python -m factory --show-config` |
| Manual issue intake | `python -m factory intake` |
| One-shot task run | `python -m factory run` |
| Automatic worker | `python -m factory watch` |
| Current operator snapshot | `python -m factory status` |
| Phone view (loopback) | `python -m factory status --serve` |
| Retry queued Trello events | `python -m factory sync-status` |

The read-only phone view is at `/factory/status` (add `?task_id=<id>` for one
task). It binds to loopback only; use a trusted, authenticated reverse proxy or
tunnel for remote phone access. JSON is available with `Accept: application/json`.
No write routes exist. The page uses allowlisted identifiers, timestamps and
deterministic evidence; agent summaries, task bodies, provider payloads and
workspace paths are omitted. `STALLED` is derived from the persisted heartbeat
and configured missed interval, leaving the task's lifecycle status `RUNNING`.

When the Trello card ID, key and token are configured, meaningful transitions
are queued transactionally and delivered during `run`/`watch` and by the status
server's once-per-minute monitor. `sync-status` retries queued events after a
restart. Heartbeats do not enqueue events. Stall and recovery observations are
deduplicated per run heartbeat. Delivery failure leaves the event pending and
does not interrupt an agent run. A separate alert channel can implement the
`AlertChannel` port; the Trello implementation comments on the card only for
`WAITING_HUMAN`, `FAILED`, `STALLED` and `DONE`, never heartbeat updates. Another
channel can implement the same port later. Delivery is at least once after a
crash between the remote write and the local acknowledgement.

`pyproject.toml` sets `testpaths = ["tests"]` and `pythonpath = ["src"]`, so
`pytest` works from the repository root without installing the package.

All four checks (tests, lint, format, types) must pass before a change is
proposed.

### Cache-neutral gates for cross-UID shared workspaces

When the Factory validates a workspace produced by OpenHands in a different UID,
OpenHands leaves ignored/private caches (`.pytest_cache/`, `.ruff_cache/`,
`.mypy_cache/`) owner-only, so host-side gates must not need to write into them.
The self-development validation profile uses cache-neutral invocations:

```bash
pytest -p no:cacheprovider
ruff check --no-cache .
ruff format --check --no-cache .
mypy --cache-dir=/dev/null
```

These keep the private ignored cache contents opaque and avoid a gate that would
require group write to an owner-only directory. The permission policy itself is
in `integrations/workspace/shared_policy.py`; see
[`openhands-shared-workspace-runtime.md`](openhands-shared-workspace-runtime.md).

## Project layout

```
src/factory/
├── domain/          # pure typed model — no I/O; IssueSource/TaskRepository/RunRepository ports
├── orchestration/   # lifecycle, intake service, dispatch service, transition service
├── integrations/    # github/ (read-only IssueSource); agent adapters later
└── infrastructure/  # configuration, logging, persistence/ (SQLite)
tests/               # pure-logic tests, no network or real environment
```

## Layering rules

Dependencies point inward: `infrastructure` and `integrations` depend on
`orchestration`, which depends on `domain`. Never the reverse.

- `domain` must not import from any other `factory` subpackage and must not
  perform I/O. It declares the contracts (`IssueSource`, `TaskRepository`,
  `RunRepository`) that other layers implement.
- `orchestration` must not import a concrete agent engine, the GitHub client or
  SQLite — only protocols/ports and pure logic from `domain`.
- `infrastructure` must not import `orchestration` (nor `integrations`).

`tests/test_layering.py` enforces these rules by parsing imports, so a wrong-way
dependency fails the suite rather than being caught in review.

If a change seems to require breaking one of these, the design is wrong; raise
it rather than working around it.

## Adding code

1. Start from a feature branch. Never commit to `main`.
2. Put new logic in the layer that owns it (see the table above).
3. Add or update tests for behavior, not implementation details.
4. Run all four checks locally.
5. Open a Pull Request. **Do not merge it.**

## Testing conventions

- Tests cover real logic — no mocks of the code under test. Persistence tests
  use a real temporary SQLite file; intake tests use a real repository and a fake
  `IssueSource` implementation, not a mock.
- No network access and no reading of the real environment. Configuration tests
  pass an explicit mapping to `FactoryConfig.from_env({...})`; CLI tests set a
  scoped `os.environ` through monkeypatch after clearing ambient variables.
- The GitHub adapter is tested against an injected in-memory transport, so
  pagination, filtering and error handling run deterministically offline.
- No test may depend on live GitHub.
- Keep tests deterministic: no reliance on wall-clock ordering or external state.
- If a required dependency for testing is missing, raise it before installing a
  large stack.

## Change workflow

```bash
git checkout main && git pull
git checkout -b <type>/<short-description>     # e.g. feat/issue-intake

# ... make changes ...

pytest && ruff check . && ruff format --check . && mypy

git add <files> && git commit -m "<clear, imperative message>"
git push -u origin <branch>
gh pr create --fill
```

Then stop. A human reviews and merges. The agent that opened a PR never merges
it.

## Definition of done

- All four checks pass.
- Behavior is covered by tests.
- Layering rules are respected.
- Documentation (`README.md`, `docs/`, `AGENTS.md`) is updated if behavior or
  architecture changed.
- No secret, no product code from `finanza-ia`, and no host-infrastructure change
  is included.

## Explicit retry after failed dispatch

After resolving the underlying blocker, a human can run:

```bash
python -m factory retry --task-id <uuid>
```

This command requires only local database configuration. It accepts `BLOCKED`
tasks without an active run and records `BLOCKED → READY`. It also accepts a
legacy `CLAIMED` task only when it has historical runs, the most recent run is
`FAILED`, and no active run exists. Recovery checks the exact `CLAIMED` state
and records `CLAIMED → BLOCKED → READY` through the normal lifecycle. It performs
no intake, agent invocation, workspace reuse or historical run updates. A repeated request
or any other status fails clearly. A subsequent manual runtime invocation can
create a fresh attempt; retry itself starts no work.

Legacy recovery is explicit, never automatic: the command preserves the failed
run, its workspace and all prior history. A `CLAIMED` task with no runs, a latest
run other than `FAILED`, or any active run is refused without a retry transition.
Concurrent retries use guarded lifecycle writes and cannot duplicate recovery
edges or create runs.

The production retry policy reads the failed run history and last attempt time
from SQLite. It refuses a fourth failed run attempt and delays eligible retries
by 60 seconds after the first failure and 120 seconds after the second.
`BACKOFF_PENDING` means the watcher will wait; it has not created a run. A task
with a successful agent run but failed quality gates enters the existing QA
correction path on the same checkout. That path allows two correction runs,
with the same delay, then records `BLOCKED` and a reason if the gates still
fail. A human `request-changes` cycle continues to use the reviewed PR and
branch. The worker never merges that PR.

Each `run` or `watch` pass first reconciles persisted tasks against their latest
run. It repairs a missing terminal lifecycle transition through the normal
tracking service. A `CLAIMED` task with no run stays claimed and is reported as
unresolved: the operator must verify that no external agent was started before
choosing a recovery action. The durable claim prevents another task from
starting in the meantime. An active run that cannot be collected stays active;
the worker observes that same run on a later pass.

### Operator-evidenced terminal Codex timeout recovery

A CODE task recorded FAILED remains terminal for ordinary scheduling and
`factory retry`. After inspecting the actual Codex attempt, an operator may
explicitly authorize **one** exceptional timeout recovery:

```sh
python -m factory recover-timeout --task-id TASK_UUID --run-id FAILED_RUN_UUID --acknowledge-timeout
python -m factory retry --task-id TASK_UUID
python -m factory sprint resume --sprint-id APPROVED_SPRINT_ID
```

The operator must independently confirm that the worker was killed at the
configured timeout; `exit_code=-9` alone could also be an external SIGKILL.
The exceptional command requires the exact latest FAILED CODEX run, its trusted
worker result (`status=FAILED`, `exit_code=-9`), the existing isolated checkout
and canonical Factory branch, an eligible source Issue, authorization in the
current Sprint when enabled, no active run, no recorded local PR, and no open
provider PR on that branch. Provider failures are fail-closed. The number of
failed runs must remain below the bounded recovery policy limit.

Success records a special guarded, atomic FAILED -> BLOCKED transition and a
normal append-only E1 audit fact. This exception is not exposed through the
ordinary state machine. Nothing is dispatched, erased, merged, deployed or
automatically resumed. The existing explicit retry and human Sprint resume are
both still required. The subsequent Codex run **reuses the exact original
workspace/branch** and has a fresh AgentRun; its predecessor remains FAILED in
history. Existing QA gates and PR WAITING_HUMAN safeguards still apply.

A branch with an existing PR requires human PR review, not terminal timeout
recovery. In particular, the manually published E4 PR #68 must be reviewed on
its own merits; this feature must not alter E4's historical FAILED/PAUSED
production records merely to make the dashboard appear complete.

Terminal recovery requires the new worker's explicit `timed_out: true` result evidence as well as exit_code -9; SIGKILL alone is not proof of a timeout. Legacy results without that marker are not eligible for automatic operator recovery and require separate human review.

### Explicit legacy Codex recovery without result evidence

For a pre-result-evidence attempt only, `factory recover-legacy --task-id TASK_UUID --run-id FAILED_RUN_UUID --acknowledge-legacy-recovery` is a separate compatibility operation. It accepts only the exact latest FAILED CODEX run with no `.result` file, a clean isolated Factory workspace with no commits ahead of base, no active run or local/provider PR, an eligible source Issue, and remaining retry budget. Project/workspace identity and Sprint authorization are checked as for other terminal recovery. It records one atomic, auditable `FAILED → READY` transition with a `terminal-legacy-recovery:<run>` marker. It does not dispatch; normal scheduling handles READY. If trusted `.result` exists, use the corresponding regular evidence-based recovery command.
