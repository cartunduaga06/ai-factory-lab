# Parallel worker runtime

The pool schedules distinct task IDs into at most two process-local worker
sessions. A session constructs its own `FactoryRuntime` and adapter. The SQLite
task, run, workspace and PR records remain authoritative; the process-local
session is discarded after a pass. After restart, active lifecycle states are
selected before READY work and the persisted run is resumed. Dispatch uses a
SQLite claim transaction that counts distinct active task/run identities up to
the configured pool capacity, the task status compare-and-swap, and the
active-run uniqueness constraint. One-shot `run` and `watch` retain capacity
one. The Git
provisioner gives each run a unique worktree path and branch. Project routing
continues to resolve the task's registered repository before dispatch.

If publication raises after a pool worker has a successful, validated latest
run, the worker records `VALIDATING → BLOCKED` for that task with a sanitized
reason naming its exact run and workspace. The original run, green gates,
security review and transition history remain available for inspection. The
pool reports the exception class for that session and continues its peers.
`BLOCKED` is excluded from automatic selection after restart, so an unchanged
workspace cannot be published repeatedly. Operators must resolve the
publication blocker and explicitly authorize recovery; the publisher still
refuses branches containing only an agent-created commit. One-shot `run` and
`watch` keep their existing publication error behavior.

Use `FACTORY_MAX_CONCURRENCY=2 python -m factory pool` after configuring
the normal runtime credentials, project profiles and quality gates. `1` is the
supported serial setting. The historical `factory watch` command is a
compatibility alias for this same continuous pool supervisor. Both entrypoints
reconcile the configured Trello backlog before GitHub intake on every
scheduling pass. A task at `WAITING_HUMAN` remains persisted for review and
does not stop intake or an independent worker. `SIGINT` and `SIGTERM` stop new
scheduling, wake any idle wait immediately, request cancellation for active
agent runs through the engine adapter, and then drain the affected sessions.
Results print task ID, run ID, branch, gate result, PR and outcome; inspect the
SQLite task transitions, run/workspace and PR rows for the complete trace. No
pool path merges or deploys.

For an acceptance run, authorize two independent real Issues, one for this
repository and one for Dulces El Jericoano, with separate project profiles and
source checkouts. Record overlapping run start/end times, distinct repository
slugs, worktree paths, branches and run IDs, each Issue-to-PR link, gate results,
and both final `WAITING_HUMAN` states. Then fail or cancel one worker in a
separate run and verify the other continues; restart the orchestrator while
workers are active and verify each resumes its persisted run. Stop without
merging either PR.

The Trello Sprint authorizes only its current ordered step. In pool mode, a
GitHub-direct Issue is independently authorized by its `factory-ready` intake
eligibility and durable direct origin; eligibility is checked again before
dispatch. A task linked to a Trello WorkItem cannot use this direct route, and
an inactive Sprint step stays ineligible. Thus two independently authorized
GitHub-direct Issues can run while a Sprint is configured. The real Dulces task
and PR evidence must be supplied by an authorized environment with that
repository and credentials; local tests alone cannot satisfy the final E2E gate.
