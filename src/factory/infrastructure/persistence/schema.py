"""SQLite schema for Phase 2A persistence.

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
"""

from __future__ import annotations

SCHEMA_VERSION = 1

TASKS_TABLE = "tasks"
TRANSITIONS_TABLE = "transitions"

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

CREATE_TASKS_STATUS_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TASKS_TABLE}_status ON {TASKS_TABLE} (status);"
)

CREATE_TASKS_CREATED_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TASKS_TABLE}_created_at ON {TASKS_TABLE} (created_at);"
)

CREATE_TRANSITIONS_TASK_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TRANSITIONS_TABLE}_task_id ON {TRANSITIONS_TABLE} (task_id);"
)

#: Statements applied, in order, by :func:`initialize_schema`.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS,
    CREATE_TRANSITIONS,
    CREATE_TASKS_STATUS_INDEX,
    CREATE_TASKS_CREATED_INDEX,
    CREATE_TRANSITIONS_TASK_INDEX,
)

__all__ = [
    "CREATE_TASKS",
    "CREATE_TRANSITIONS",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "TASKS_TABLE",
    "TRANSITIONS_TABLE",
]
