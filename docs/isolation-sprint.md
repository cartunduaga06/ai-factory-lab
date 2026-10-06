# Isolation sprint: staging, release and rollback

## Process-level staging

The staging runtime is intentionally process isolated; Python and SQLite do not
benefit from adding a second Docker control plane. Install the project in the
existing staging venv, keep credentials in the operator-owned
`/srv/ai-factory/staging/vnext.env`, and start a command with
`ops/staging/run <factory-command>`. This wrapper pins the database to
`/srv/ai-factory/staging/state/factory.db`, worktrees to
`/srv/ai-factory/staging/workspaces`, logs to `/srv/ai-factory/staging/logs`,
and status to port `8865`. The wrapper overrides path and port values after
loading the env file. Do not copy credentials into this repository.

Use `ops/staging/health` for a config and local status check. Use
`ops/staging/health --serve` to bind the read-only status endpoint on loopback
port 8865. Non-production config also refuses known production checkout,
state, workspace and log roots.

## Workspace and job ownership evidence

Each run gets its own generated workspace id, branch and path. SQLite enforces
one active run per task and one active run per workspace; workspace identity is
immutable once attached. Pool-created sessions bind a worker id and unique
session id to the persisted run. Existing rows migrate with nullable owner
fields and remain readable. `ops/harness/soak` runs deterministic local tests
for persistence, two-worker overlap and exception isolation; all provider
fixtures are fake and the workspace test uses temporary repositories.

For later real two-worker E2E, first configure two staging-eligible independent
fixture tasks and fake/isolated providers. Evidence must include the exact
staging commit/config fingerprint, distinct task/run/worker/session ids,
distinct workspace ids/paths/branches, overlap timestamps, isolated file
contents, terminal task/run states, and proof that an injected failure leaves
the peer complete and the failed task blocked/otherwise fail-closed. Never use
Finanza IA or live product state for this harness.

## Development through release candidate

1. Work on a feature branch; keep changes local until reviewed.
2. Run `pytest`, `ruff check .`, `ruff format --check .`, `mypy`, and
   `git diff --check`.
3. Run staging smoke/soak with `ops/staging/health` and
   `ops/harness/soak`; retain output and the isolated DB/workspace identities.
4. Commit the reviewed candidate and run `ops/release/prepare-rc` from the
   clean branch. It runs every code gate and prints the exact commit SHA,
   branch and human-gate status. Attach staging smoke/soak evidence and any
   limitations to the report. A release candidate is an immutable commit
   record, not a deployment.
5. Stop at **HUMAN GATE** for review and production promotion authorization.
   No command in this repository merges or deploys automatically.

## Rollback

`ops/release/rollback-reference <tag-or-commit>` resolves and prints an
immutable commit candidate. It does not checkout, reset, push, alter the
production tree or initiate rollback. A human operator must select the target,
review impact and execute any separately authorized rollback procedure.

## RC readiness

The report may say READY FOR PRODUCTION PROMOTION only after all required gates
are green and the RC record identifies the exact commit. It does not authorize
promotion, merge or deployment. Any missing gate or E2E evidence means NOT
READY.

## Staging lifecycle

The reproducible lifecycle is:

    ops/staging/start       # starts the staging watcher; PID is persisted in staging/state/factory.pid
    ops/staging/status      # reports process state and durable factory state
    ops/staging/restart     # graceful stop followed by a new staging process
    ops/staging/stop        # SIGTERM with bounded wait; never sends SIGKILL
    ops/staging/health      # config/database/status smoke check

The lifecycle scripts hard-code the staging state, workspace, log and port roots
and set FACTORY_ENV=staging. Provider credentials are intentionally not stored
in vnext.env; a provider-backed watcher therefore fails closed when credentials
are absent, while local fake-provider E2E and all quality gates remain runnable.
