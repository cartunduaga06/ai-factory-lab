"""SQLite schema for factory persistence.

Kept separate from the repository implementation so the DDL reads as a single
reviewable artefact. Initialization is idempotent: every statement is
``IF NOT EXISTS`` and re-running it on an existing database is a no-op.

Design notes:

* ``tasks`` carries the structured source identity as three columns, with a
  ``UNIQUE`` constraint over ``(source_provider, source_repository,
  source_issue_number)``. This is the defense-in-depth duplicate guard: even if
  an application-level check is bypassed, the database refuses a second row for
  the same source. SQLite treats ``NULL`` as distinct in unique indexes, so
  source-less tasks never collide with each other — which is the behaviour we
  want.
* ``transitions`` records the append-only lifecycle history, linked to its task
  by foreign key with ``ON DELETE CASCADE``.
* ``workspaces`` holds the isolated checkout a run operates in. ``branch`` is
  ``NOT NULL`` because the domain rejects a workspace without one.
* ``agent_runs`` records one dispatch attempt. ``adapter`` stores the
  :class:`~factory.domain.enums.AgentKind` value, and ``workspace_id`` links the
  run to its workspace. Two partial unique indexes guard the invariants:
  ``uq_agent_runs_active_task`` allows at most one *active* (non-terminal) run
  per task — the storage-level guard behind dispatch idempotency — and
  ``uq_agent_runs_workspace`` allows at most one run per non-null workspace, the
  storage-level guard behind the Phase 4 one-workspace-per-run isolation
  invariant. Terminal statuses are excluded from the active-run index, so retries
  after a finished run are still possible.
* ``pull_requests`` records the PR the factory opened for a run. ``run_id`` is
  ``UNIQUE`` (one PR per run) and ``(repository_slug, head_branch)`` is
  ``UNIQUE`` (one active publication identity per branch), the storage-level
  guards behind publication idempotency. Initialization only adds tables and
  indexes: it never drops or rewrites existing data.
"""

from __future__ import annotations

SCHEMA_VERSION = 3

TASKS_TABLE = "tasks"
TRANSITIONS_TABLE = "transitions"
WORKSPACES_TABLE = "workspaces"
AGENT_RUNS_TABLE = "agent_runs"
PULL_REQUESTS_TABLE = "pull_requests"

#: Run statuses that count as "active" for the one-active-run-per-task guard.
#: These must match the non-terminal members of
#: :class:`factory.domain.enums.RunStatus`.
ACTIVE_RUN_STATUSES: tuple[str, ...] = ("PENDING", "RUNNING")

CREATE_TASKS = f"""
CREATE TABLE IF NOT EXISTS {TASKS_TABLE} (
    task_id              TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT NOT NULL DEFAULT '',
    target_repository    TEXT NOT NULL,
    source_provider      TEXT,
    source_repository    TEXT,
    source_issue_number  INTEGER,
    status               TEXT NOT NULL,
    labels               TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CONSTRAINT uq_tasks_source
        UNIQUE (source_provider, source_repository, source_issue_number)
);
"""

CREATE_TRANSITIONS = f"""
CREATE TABLE IF NOT EXISTS {TRANSITIONS_TABLE} (
    transition_id  TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    from_status    TEXT NOT NULL,
    to_status      TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    CONSTRAINT fk_transitions_task
        FOREIGN KEY (task_id) REFERENCES {TASKS_TABLE} (task_id) ON DELETE CASCADE
);
"""

CREATE_WORKSPACES = f"""
CREATE TABLE IF NOT EXISTS {WORKSPACES_TABLE} (
    workspace_id     TEXT PRIMARY KEY,
    repository_slug  TEXT NOT NULL,
    branch           TEXT NOT NULL,
    path             TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
"""

CREATE_AGENT_RUNS = f"""
CREATE TABLE IF NOT EXISTS {AGENT_RUNS_TABLE} (
    run_id        TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    adapter       TEXT NOT NULL,
    status        TEXT NOT NULL,
    workspace_id  TEXT,
    summary       TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    gates         TEXT NOT NULL DEFAULT '[]',
    validated_revision TEXT,
    created_at    TEXT NOT NULL,
    CONSTRAINT fk_agent_runs_task
        FOREIGN KEY (task_id) REFERENCES {TASKS_TABLE} (task_id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_runs_workspace
        FOREIGN KEY (workspace_id) REFERENCES {WORKSPACES_TABLE} (workspace_id)
);
"""

#: Idempotent migration for a database created before revision binding existed.
#: ``ALTER TABLE ... ADD COLUMN`` fills existing rows with ``NULL`` (the correct
#: "no validated revision" value) and touches no other data. A fresh database
#: already has the column, so the statement is tolerant of the duplicate-column
#: error rather than version-gated.
MIGRATE_AGENT_RUNS_VALIDATED_REVISION = (
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN validated_revision TEXT;"
)

CREATE_TASKS_STATUS_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TASKS_TABLE}_status ON {TASKS_TABLE} (status);"
)

CREATE_TASKS_CREATED_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TASKS_TABLE}_created_at ON {TASKS_TABLE} (created_at);"
)

CREATE_TRANSITIONS_TASK_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TRANSITIONS_TABLE}_task_id ON {TRANSITIONS_TABLE} (task_id);"
)

CREATE_AGENT_RUNS_TASK_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{AGENT_RUNS_TABLE}_task_id ON {AGENT_RUNS_TABLE} (task_id);"
)

#: One active run per task. Terminal runs fall outside the index, so a retry
#: after a finished run is still allowed.
CREATE_AGENT_RUNS_ACTIVE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{AGENT_RUNS_TABLE}_active_task
    ON {AGENT_RUNS_TABLE} (task_id)
 WHERE status IN ({", ".join(repr(status) for status in ACTIVE_RUN_STATUSES)});
"""

#: One workspace per run. This is the storage-level guard behind the Phase 4
#: isolation invariant: no two runs may point at the same working tree, even if
#: an application-level check is bypassed. Runs without a workspace are exempt
#: (SQLite treats NULLs as distinct), so a run that never had one is unaffected.
CREATE_AGENT_RUNS_WORKSPACE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{AGENT_RUNS_TABLE}_workspace
    ON {AGENT_RUNS_TABLE} (workspace_id)
 WHERE workspace_id IS NOT NULL;
"""

#: Pull requests the factory opened. ``run_id`` is ``UNIQUE`` so a run can have at
#: most one persisted PR — the storage guard behind publication idempotency.
#: ``merged`` is bookkeeping only: the factory never performs the merge.
CREATE_PULL_REQUESTS = f"""
CREATE TABLE IF NOT EXISTS {PULL_REQUESTS_TABLE} (
    pull_request_id  TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    task_id          TEXT,
    repository_slug  TEXT NOT NULL,
    head_branch      TEXT NOT NULL,
    base_branch      TEXT NOT NULL,
    title            TEXT NOT NULL,
    number           INTEGER,
    url              TEXT,
    merged           INTEGER NOT NULL DEFAULT 0,
    opened_at        TEXT NOT NULL,
    CONSTRAINT uq_pull_requests_run UNIQUE (run_id),
    CONSTRAINT fk_pull_requests_run
        FOREIGN KEY (run_id) REFERENCES {AGENT_RUNS_TABLE} (run_id) ON DELETE CASCADE
);
"""

CREATE_PULL_REQUESTS_RUN_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{PULL_REQUESTS_TABLE}_run_id ON {PULL_REQUESTS_TABLE} (run_id);"
)

#: One active publication identity per unique target repository/head branch. A
#: second *different* PR for the same branch is refused rather than silently
#: superseding the first, so a retry can never fork publication identity.
CREATE_PULL_REQUESTS_BRANCH_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{PULL_REQUESTS_TABLE}_repository_branch
    ON {PULL_REQUESTS_TABLE} (repository_slug, head_branch);
"""

#: Idempotent statements that bring an existing database up to date with the
#: current schema. Applied tolerantly (a duplicate column is ignored), so an old
#: Phase 4/5 database gains ``agent_runs.validated_revision`` without losing data,
#: and a fresh database (which already has it) is unaffected.
MIGRATION_STATEMENTS: tuple[str, ...] = (MIGRATE_AGENT_RUNS_VALIDATED_REVISION,)

#: Statements applied, in order, by :func:`initialize_schema`.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS,
    CREATE_TRANSITIONS,
    CREATE_WORKSPACES,
    CREATE_AGENT_RUNS,
    CREATE_PULL_REQUESTS,
    CREATE_TASKS_STATUS_INDEX,
    CREATE_TASKS_CREATED_INDEX,
    CREATE_TRANSITIONS_TASK_INDEX,
    CREATE_AGENT_RUNS_TASK_INDEX,
    CREATE_AGENT_RUNS_ACTIVE_INDEX,
    CREATE_AGENT_RUNS_WORKSPACE_INDEX,
    CREATE_PULL_REQUESTS_RUN_INDEX,
    CREATE_PULL_REQUESTS_BRANCH_INDEX,
)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "AGENT_RUNS_TABLE",
    "CREATE_AGENT_RUNS",
    "CREATE_PULL_REQUESTS",
    "CREATE_TASKS",
    "CREATE_TRANSITIONS",
    "CREATE_WORKSPACES",
    "MIGRATION_STATEMENTS",
    "PULL_REQUESTS_TABLE",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "TASKS_TABLE",
    "TRANSITIONS_TABLE",
    "WORKSPACES_TABLE",
]
