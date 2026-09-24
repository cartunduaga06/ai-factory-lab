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
  run to its workspace. The partial unique index
  ``uq_agent_runs_active_task`` allows at most one *active* (non-terminal) run
  per task: that is the storage-level guard behind dispatch idempotency.
  Terminal statuses are excluded from the index, so retries after a finished run
  are still possible.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

TASKS_TABLE = "tasks"
TRANSITIONS_TABLE = "transitions"
WORKSPACES_TABLE = "workspaces"
AGENT_RUNS_TABLE = "agent_runs"

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
    created_at    TEXT NOT NULL,
    CONSTRAINT fk_agent_runs_task
        FOREIGN KEY (task_id) REFERENCES {TASKS_TABLE} (task_id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_runs_workspace
        FOREIGN KEY (workspace_id) REFERENCES {WORKSPACES_TABLE} (workspace_id)
);
"""

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

#: Statements applied, in order, by :func:`initialize_schema`.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS,
    CREATE_TRANSITIONS,
    CREATE_WORKSPACES,
    CREATE_AGENT_RUNS,
    CREATE_TASKS_STATUS_INDEX,
    CREATE_TASKS_CREATED_INDEX,
    CREATE_TRANSITIONS_TASK_INDEX,
    CREATE_AGENT_RUNS_TASK_INDEX,
    CREATE_AGENT_RUNS_ACTIVE_INDEX,
)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "AGENT_RUNS_TABLE",
    "CREATE_AGENT_RUNS",
    "CREATE_TASKS",
    "CREATE_TRANSITIONS",
    "CREATE_WORKSPACES",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "TASKS_TABLE",
    "TRANSITIONS_TABLE",
    "WORKSPACES_TABLE",
]
