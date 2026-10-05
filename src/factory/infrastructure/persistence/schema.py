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
  ``uq_agent_runs_workspace`` allows at most one active run per workspace.
  Terminal runs may share the reviewed checkout across sequential QA cycles.
* ``pull_requests`` records the PR the factory opened for a run. ``run_id`` is
  ``UNIQUE`` (one PR per run) and ``(repository_slug, head_branch)`` is
  ``UNIQUE`` (one active publication identity per branch), the storage-level
  guards behind publication idempotency. Initialization migrates the old
  workspace uniqueness index to its active-run form without rewriting rows.
"""

from __future__ import annotations

# ruff: noqa: E501 - SQL trigger expressions are kept intact for review.

SCHEMA_VERSION = 15

TASKS_TABLE = "tasks"
TRANSITIONS_TABLE = "transitions"
WORKSPACES_TABLE = "workspaces"
AGENT_RUNS_TABLE = "agent_runs"
PULL_REQUESTS_TABLE = "pull_requests"
QA_REWORK_TABLE = "qa_rework"
STATUS_EVENTS_TABLE = "status_events"
AUDIT_EVENTS_TABLE = "audit_events"
BACKLOG_LINKS_TABLE = "backlog_links"
SPRINTS_TABLE = "sprints"

CREATE_SPRINTS = f"""
CREATE TABLE IF NOT EXISTS {SPRINTS_TABLE} (
    sprint_id TEXT PRIMARY KEY,
    manifest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'PAUSED', 'CANCELLED', 'COMPLETE')),
    position INTEGER NOT NULL CHECK (position >= 0)
);
"""

CREATE_ACTIVE_SPRINT_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{SPRINTS_TABLE}_live_project
ON {SPRINTS_TABLE} (json_extract(manifest, '$.steps[0].item.project_id'))
WHERE state IN ('ACTIVE', 'PAUSED');
"""

CREATE_SPRINT_IMMUTABLE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS sprint_manifest_immutable
BEFORE UPDATE OF manifest ON {SPRINTS_TABLE}
BEGIN SELECT RAISE(ABORT, 'sprint manifest is immutable'); END;
"""

CREATE_BACKLOG_LINKS = f"""
CREATE TABLE IF NOT EXISTS {BACKLOG_LINKS_TABLE} (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    repository_slug TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'ai-factory-lab',
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'POSTING', 'MATERIALIZED')),
    issue_number INTEGER,
    issue_url TEXT,
    PRIMARY KEY (provider, external_id),
    UNIQUE (repository_slug, issue_number)
);
"""

CREATE_BACKLOG_TASK_PROJECT_GUARD = f"""
CREATE TRIGGER IF NOT EXISTS backlog_task_project_guard
BEFORE INSERT ON {TASKS_TABLE}
WHEN NEW.source_provider = 'github' AND EXISTS (
    SELECT 1 FROM {BACKLOG_LINKS_TABLE} link
    WHERE link.state = 'MATERIALIZED'
      AND link.project_id != 'ai-factory-lab'
      AND link.repository_slug = NEW.source_repository
      AND link.issue_number = NEW.source_issue_number
      AND (link.project_id != NEW.project_id OR link.repository_slug != NEW.target_repository)
)
BEGIN SELECT RAISE(ABORT, 'backlog project identity mismatch'); END;
"""

CREATE_TASK_PROJECT_IMMUTABLE = f"""
CREATE TRIGGER IF NOT EXISTS task_project_immutable
BEFORE UPDATE ON {TASKS_TABLE}
WHEN OLD.project_id != NEW.project_id OR OLD.target_repository != NEW.target_repository
BEGIN SELECT RAISE(ABORT, 'task project identity is immutable'); END;
"""

CREATE_LINK_PROJECT_IMMUTABLE = f"""
CREATE TRIGGER IF NOT EXISTS backlog_link_project_immutable
BEFORE UPDATE ON {BACKLOG_LINKS_TABLE}
WHEN OLD.project_id != NEW.project_id OR OLD.repository_slug != NEW.repository_slug
BEGIN SELECT RAISE(ABORT, 'backlog project identity is immutable'); END;
"""

CREATE_ROUTING_REJECTIONS = """
CREATE TABLE IF NOT EXISTS routing_rejections (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    rejected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, external_id, project_id, reason)
);
"""

# Audit rows are projections of committed facts, written by triggers in the same
# transaction. A task id is the stable trace id; event_seq orders concurrent
# facts within that trace without depending on wall-clock precision.
CREATE_AUDIT_EVENTS = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_EVENTS_TABLE} (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    correlation_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT,
    workspace_id TEXT,
    pull_request_id TEXT,
    causation_id TEXT NOT NULL,
    source_provider TEXT,
    source_repository TEXT,
    source_issue_number INTEGER,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{{}}',
    UNIQUE (correlation_id, event_seq)
);
"""

CREATE_AUDIT_INDEX = f"""
CREATE INDEX IF NOT EXISTS ix_{AUDIT_EVENTS_TABLE}_trace
ON {AUDIT_EVENTS_TABLE} (correlation_id, event_seq);
"""


def _audit_insert(
    name: str,
    key: str,
    task: str,
    cause: str,
    aggregate: str,
    aggregate_id: str,
    occurred: str,
    run: str = "NULL",
    workspace: str = "NULL",
    pr: str = "NULL",
    condition: str = "1",
) -> str:
    return f"""INSERT INTO {AUDIT_EVENTS_TABLE}
    (event_key, correlation_id, event_seq, name, task_id, run_id, workspace_id,
     pull_request_id, causation_id, source_provider, source_repository,
     source_issue_number, aggregate_type, aggregate_id, aggregate_version, occurred_at)
    SELECT {key}, {task},
      (SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} WHERE correlation_id = {task}),
      {name}, {task}, {run}, {workspace}, {pr}, {cause},
      t.source_provider, t.source_repository, t.source_issue_number,
      '{aggregate}', {aggregate_id},
      (SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE}
       WHERE aggregate_type = '{aggregate}' AND aggregate_id = {aggregate_id}),
      {occurred} FROM {TASKS_TABLE} t WHERE t.task_id = {task} AND ({condition});"""


CREATE_AUDIT_TASK_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_task_materialized AFTER INSERT ON {TASKS_TABLE}
BEGIN
{_audit_insert("'IssueMaterialized'", "'task:' || NEW.task_id", "NEW.task_id", "NEW.task_id", "task", "NEW.task_id", "NEW.created_at")}
{_audit_insert("'WorkItemReady'", "'task:ready:' || NEW.task_id", "NEW.task_id", "NEW.task_id", "task", "NEW.task_id", "NEW.created_at", condition="NEW.status = 'READY'")}
END;
"""

CREATE_AUDIT_TRANSITION_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_transition AFTER INSERT ON {TRANSITIONS_TABLE}
BEGIN
{_audit_insert("CASE NEW.to_status WHEN 'READY' THEN 'WorkItemReady' WHEN 'CLAIMED' THEN 'TaskClaimed' WHEN 'PR_OPEN' THEN CASE WHEN (SELECT count(*) FROM transitions WHERE task_id = NEW.task_id AND to_status = 'PR_OPEN') > 1 THEN 'PRUpdated' ELSE 'TaskPR_OPEN' END WHEN 'WAITING_HUMAN' THEN 'HumanApprovalRequired' WHEN 'CHANGES_REQUESTED' THEN 'ChangesRequested' WHEN 'DONE' THEN 'TaskCompleted' ELSE 'Task' || NEW.to_status END", "'transition:' || NEW.transition_id", "NEW.task_id", "NEW.transition_id", "task", "NEW.task_id", "NEW.occurred_at", "(SELECT run_id FROM agent_runs WHERE task_id = NEW.task_id ORDER BY created_at DESC, rowid DESC LIMIT 1)", "(SELECT workspace_id FROM agent_runs WHERE task_id = NEW.task_id ORDER BY created_at DESC, rowid DESC LIMIT 1)", "(SELECT pull_request_id FROM pull_requests WHERE task_id = NEW.task_id ORDER BY rowid DESC LIMIT 1)")}
END;
"""

CREATE_AUDIT_RUN_INSERT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_run_insert AFTER INSERT ON {AGENT_RUNS_TABLE}
BEGIN
{_audit_insert("'RunStarted'", "'run:start:' || NEW.run_id", "NEW.task_id", "NEW.run_id", "run", "NEW.run_id", "COALESCE(NEW.started_at, NEW.created_at)", "NEW.run_id", "NEW.workspace_id")}
END;
"""

CREATE_AUDIT_CONTEXT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_context_pack AFTER INSERT ON {AGENT_RUNS_TABLE}
WHEN NEW.context_pack IS NOT NULL
BEGIN
{_audit_insert("'ContextPackBuilt'", "'context:' || NEW.run_id", "NEW.task_id", "NEW.run_id", "run", "NEW.run_id", "NEW.created_at", "NEW.run_id", "NEW.workspace_id")}
END;
"""

CREATE_AUDIT_RUN_FINISH_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_run_finish AFTER UPDATE OF status ON {AGENT_RUNS_TABLE}
WHEN OLD.status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
 AND NEW.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
BEGIN
{_audit_insert("'RunFinished'", "'run:finish:' || NEW.run_id", "NEW.task_id", "NEW.run_id", "run", "NEW.run_id", "COALESCE(NEW.finished_at, strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))", "NEW.run_id", "NEW.workspace_id")}
END;
"""

CREATE_AUDIT_VALIDATION_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_validation AFTER UPDATE ON {AGENT_RUNS_TABLE}
WHEN NEW.status = 'SUCCEEDED' AND
 (OLD.status != 'SUCCEEDED' OR OLD.gates != NEW.gates OR
  OLD.validated_revision IS NOT NEW.validated_revision)
BEGIN
{_audit_insert("CASE WHEN EXISTS (SELECT 1 FROM json_each(NEW.gates) WHERE json_extract(value, '$.required') = 1 AND json_extract(value, '$.status') != 'PASSED') THEN 'ValidationFailed' ELSE 'ValidationPassed' END", "'validation:' || NEW.run_id || ':' || CASE WHEN EXISTS (SELECT 1 FROM json_each(NEW.gates) WHERE json_extract(value, '$.required') = 1 AND json_extract(value, '$.status') != 'PASSED') THEN 'failed' ELSE 'passed' END", "NEW.task_id", "NEW.run_id", "run", "NEW.run_id", "strftime('%Y-%m-%dT%H:%M:%f+00:00','now')", "NEW.run_id", "NEW.workspace_id").replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)}
END;
"""

CREATE_AUDIT_PR_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_pr_insert AFTER INSERT ON {PULL_REQUESTS_TABLE}
BEGIN
{_audit_insert("'PRCreated'", "'pr:create:' || NEW.pull_request_id", "(SELECT task_id FROM agent_runs WHERE run_id = NEW.run_id)", "NEW.run_id", "pull_request", "NEW.pull_request_id", "NEW.opened_at", "NEW.run_id", "(SELECT workspace_id FROM agent_runs WHERE run_id = NEW.run_id)", "NEW.pull_request_id")}
END;
"""

CREATE_AUDIT_PR_UPDATE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS audit_pr_update AFTER UPDATE ON {PULL_REQUESTS_TABLE}
WHEN OLD.number IS NOT NEW.number OR OLD.url IS NOT NEW.url OR
 OLD.merged IS NOT NEW.merged OR OLD.commit_sha IS NOT NEW.commit_sha OR
 OLD.run_id IS NOT NEW.run_id
BEGIN
{_audit_insert("'PRUpdated'", "'pr:update:' || NEW.pull_request_id || ':' || (SELECT count(*) + 1 FROM audit_events WHERE aggregate_type = 'pull_request' AND aggregate_id = NEW.pull_request_id)", "(SELECT task_id FROM agent_runs WHERE run_id = NEW.run_id)", "NEW.run_id", "pull_request", "NEW.pull_request_id", "strftime('%Y-%m-%dT%H:%M:%f+00:00','now')", "NEW.run_id", "(SELECT workspace_id FROM agent_runs WHERE run_id = NEW.run_id)", "NEW.pull_request_id")}
END;
"""

CREATE_AUDIT_NO_UPDATE = f"""
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON {AUDIT_EVENTS_TABLE}
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
"""

CREATE_AUDIT_NO_DELETE = f"""
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON {AUDIT_EVENTS_TABLE}
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
"""

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
    project_id           TEXT NOT NULL DEFAULT 'ai-factory-lab',
    source_provider      TEXT,
    source_repository    TEXT,
    source_issue_number  INTEGER,
    status               TEXT NOT NULL,
    labels               TEXT NOT NULL DEFAULT '[]',
    kind                 TEXT NOT NULL DEFAULT 'CODE',
    blocked_reason       TEXT,
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

CREATE_STATUS_EVENTS = f"""
CREATE TABLE IF NOT EXISTS {STATUS_EVENTS_TABLE} (
    transition_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    delivered_at TEXT
);
"""

CREATE_STATUS_EVENT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS queue_status_transition
AFTER INSERT ON {TRANSITIONS_TABLE}
WHEN NEW.to_status IN (
    'CLAIMED', 'RUNNING', 'VALIDATING', 'PR_OPEN', 'WAITING_HUMAN',
    'FAILED', 'BLOCKED', 'DONE', 'CANCELLED', 'CHANGES_REQUESTED'
)
BEGIN
    INSERT INTO {STATUS_EVENTS_TABLE} (transition_id, task_id, phase, occurred_at)
    VALUES (NEW.transition_id, NEW.task_id, NEW.to_status, NEW.occurred_at);
END;
"""

CREATE_WORKSPACES = f"""
CREATE TABLE IF NOT EXISTS {WORKSPACES_TABLE} (
    workspace_id     TEXT PRIMARY KEY,
    repository_slug  TEXT NOT NULL,
    branch           TEXT NOT NULL,
    path             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'CODE',
    created_at       TEXT NOT NULL
);
"""

CREATE_AGENT_RUNS = f"""
CREATE TABLE IF NOT EXISTS {AGENT_RUNS_TABLE} (
    run_id        TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    project_id    TEXT NOT NULL DEFAULT 'ai-factory-lab',
    adapter       TEXT NOT NULL,
    status        TEXT NOT NULL,
    workspace_id  TEXT,
    summary       TEXT,
    started_at    TEXT,
    last_heartbeat TEXT,
    agent_heartbeat TEXT,
    finished_at   TEXT,
    gates         TEXT NOT NULL DEFAULT '[]',
    validated_revision TEXT,
    context_pack TEXT,
    worker_id TEXT,
    session_id TEXT,
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

#: A reviewed checkout may be reused by sequential rework runs. Concurrent
#: runs on that checkout are still forbidden at storage level.
CREATE_AGENT_RUNS_WORKSPACE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{AGENT_RUNS_TABLE}_workspace
    ON {AGENT_RUNS_TABLE} (workspace_id)
 WHERE workspace_id IS NOT NULL AND status IN
       ({", ".join(repr(status) for status in ACTIVE_RUN_STATUSES)});
"""

CREATE_AGENT_RUNS_WORKSPACE_OWNER_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS trg_{AGENT_RUNS_TABLE}_workspace_owner
BEFORE INSERT ON {AGENT_RUNS_TABLE}
WHEN NEW.workspace_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM {AGENT_RUNS_TABLE}
    WHERE workspace_id = NEW.workspace_id AND task_id != NEW.task_id
)
BEGIN
    SELECT RAISE(ABORT, 'workspace owner mismatch');
END;
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
    commit_sha       TEXT,
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

CREATE_QA_REWORK = f"""
CREATE TABLE IF NOT EXISTS {QA_REWORK_TABLE} (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    reviewed_run_id TEXT NOT NULL UNIQUE,
    feedback TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES {TASKS_TABLE} (task_id) ON DELETE CASCADE,
    FOREIGN KEY (reviewed_run_id) REFERENCES {AGENT_RUNS_TABLE} (run_id)
);
"""

#: Idempotent statements that bring an existing database up to date with the
#: current schema. Applied tolerantly (a duplicate column is ignored), so an old
#: Phase 4/5 database gains ``agent_runs.validated_revision`` without losing data,
#: and a fresh database (which already has it) is unaffected.
MIGRATION_STATEMENTS: tuple[str, ...] = (
    MIGRATE_AGENT_RUNS_VALIDATED_REVISION,
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN context_pack TEXT;",
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN last_heartbeat TEXT;",
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN agent_heartbeat TEXT;",
    f"ALTER TABLE {TASKS_TABLE} ADD COLUMN kind TEXT NOT NULL DEFAULT 'CODE';",
    f"ALTER TABLE {TASKS_TABLE} ADD COLUMN blocked_reason TEXT;",
    f"ALTER TABLE {WORKSPACES_TABLE} ADD COLUMN kind TEXT NOT NULL DEFAULT 'CODE';",
    f"ALTER TABLE {TASKS_TABLE} ADD COLUMN project_id TEXT NOT NULL DEFAULT 'ai-factory-lab';",
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN project_id TEXT NOT NULL DEFAULT 'ai-factory-lab';",
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN worker_id TEXT;",
    f"ALTER TABLE {AGENT_RUNS_TABLE} ADD COLUMN session_id TEXT;",
    f"ALTER TABLE {BACKLOG_LINKS_TABLE} ADD COLUMN project_id TEXT NOT NULL DEFAULT 'ai-factory-lab';",
    f"ALTER TABLE {PULL_REQUESTS_TABLE} ADD COLUMN commit_sha TEXT;",
    f"ALTER TABLE {TASKS_TABLE} ADD COLUMN reconciliation_origin TEXT;",
    f"ALTER TABLE {TASKS_TABLE} ADD COLUMN expected_work_item_id TEXT;",
)

#: Statements applied, in order, by :func:`initialize_schema`.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TASKS,
    CREATE_SPRINTS,
    CREATE_ACTIVE_SPRINT_INDEX,
    CREATE_SPRINT_IMMUTABLE_TRIGGER,
    CREATE_BACKLOG_LINKS,
    CREATE_TASK_PROJECT_IMMUTABLE,
    CREATE_LINK_PROJECT_IMMUTABLE,
    CREATE_BACKLOG_TASK_PROJECT_GUARD,
    CREATE_ROUTING_REJECTIONS,
    CREATE_AUDIT_EVENTS,
    CREATE_TRANSITIONS,
    CREATE_STATUS_EVENTS,
    CREATE_STATUS_EVENT_TRIGGER,
    CREATE_WORKSPACES,
    CREATE_AGENT_RUNS,
    CREATE_PULL_REQUESTS,
    CREATE_QA_REWORK,
    CREATE_TASKS_STATUS_INDEX,
    CREATE_TASKS_CREATED_INDEX,
    CREATE_TRANSITIONS_TASK_INDEX,
    CREATE_AGENT_RUNS_TASK_INDEX,
    CREATE_AGENT_RUNS_ACTIVE_INDEX,
    CREATE_AGENT_RUNS_WORKSPACE_INDEX,
    CREATE_AGENT_RUNS_WORKSPACE_OWNER_TRIGGER,
    CREATE_PULL_REQUESTS_RUN_INDEX,
    CREATE_PULL_REQUESTS_BRANCH_INDEX,
    CREATE_AUDIT_INDEX,
    CREATE_AUDIT_TASK_TRIGGER,
    CREATE_AUDIT_TRANSITION_TRIGGER,
    CREATE_AUDIT_RUN_INSERT_TRIGGER,
    CREATE_AUDIT_CONTEXT_TRIGGER,
    CREATE_AUDIT_VALIDATION_TRIGGER,
    CREATE_AUDIT_RUN_FINISH_TRIGGER,
    CREATE_AUDIT_PR_TRIGGER,
    CREATE_AUDIT_PR_UPDATE_TRIGGER,
    CREATE_AUDIT_NO_UPDATE,
    CREATE_AUDIT_NO_DELETE,
)

__all__ = [
    "BACKLOG_LINKS_TABLE",
    "ACTIVE_RUN_STATUSES",
    "AGENT_RUNS_TABLE",
    "CREATE_AGENT_RUNS",
    "CREATE_PULL_REQUESTS",
    "CREATE_TASKS",
    "CREATE_TRANSITIONS",
    "CREATE_WORKSPACES",
    "MIGRATION_STATEMENTS",
    "PULL_REQUESTS_TABLE",
    "QA_REWORK_TABLE",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "STATUS_EVENTS_TABLE",
    "TASKS_TABLE",
    "TRANSITIONS_TABLE",
    "WORKSPACES_TABLE",
]
