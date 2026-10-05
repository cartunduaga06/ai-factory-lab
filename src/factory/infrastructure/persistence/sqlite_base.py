"""Shared SQLite plumbing for the persistence adapters.

Both repositories open a fresh connection per operation and apply the same
schema, so the connection setup and idempotent initialization live here rather
than being duplicated. Keeping one connection per call makes a repository
trivially safe to share across threads and makes durability testable by simply
re-instantiating it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from factory.infrastructure.persistence.schema import (
    AGENT_RUNS_TABLE,
    AUDIT_EVENTS_TABLE,
    MIGRATION_STATEMENTS,
    PULL_REQUESTS_TABLE,
    SCHEMA_STATEMENTS,
    SPRINTS_TABLE,
    TASKS_TABLE,
    TRANSITIONS_TABLE,
)


class SqliteRepository:
    """Base class holding the file path, connection factory and schema setup."""

    def __init__(self, path: str, *, read_only: bool = False) -> None:
        # ``:memory:`` is honoured; any other value is a filesystem path.
        self._path = path
        self._read_only = read_only

    @property
    def path(self) -> str:
        """The database location this repository reads and writes."""
        return self._path

    def initialize(self) -> None:
        """Create the schema if it does not exist. Safe to call repeatedly.

        Idempotent ``CREATE ... IF NOT EXISTS`` statements add any missing table
        or index. The migration statements then bring an older database up to
        date — for example adding ``agent_runs.validated_revision`` — and are
        applied tolerantly, since a column that already exists (or a fresh
        database that was created with it) would otherwise raise a duplicate-column
        error. The workspace index is replaced by its active-run variant;
        stored task and run rows are not rewritten.
        """
        if self._read_only:
            raise ValueError("read-only repository cannot be initialized")
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # Sprint authorization is project-scoped; remove the pre-205 global index
            # before creating the replacement index below.
            conn.execute(f"DROP INDEX IF EXISTS uq_{SPRINTS_TABLE}_live")
            # Rework runs are sequential attempts on the same reviewed checkout.
            # The active-task index still forbids concurrent writers.
            conn.execute("DROP INDEX IF EXISTS uq_agent_runs_workspace")
            conn.execute("DROP TRIGGER IF EXISTS audit_pr_update")
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            for statement in MIGRATION_STATEMENTS:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    # The column already exists (a fresh database, or a repeat
                    # initialization). Nothing to migrate; leave the data alone.
                    continue
            # Capture existing backlog provenance once. Later link damage must
            # never turn a Trello task into a GitHub-direct delivery.
            conn.execute(
                "UPDATE tasks SET reconciliation_origin = CASE WHEN EXISTS ("
                "SELECT 1 FROM backlog_links b WHERE b.repository_slug = tasks.source_repository "
                "AND b.issue_number = tasks.source_issue_number "
                "AND b.project_id = tasks.project_id "
                "AND b.provider = 'trello' AND b.state = 'MATERIALIZED') "
                "THEN 'trello' ELSE 'github-direct' END, "
                "expected_work_item_id = (SELECT b.external_id FROM backlog_links b "
                "WHERE b.repository_slug = tasks.source_repository "
                "AND b.issue_number = tasks.source_issue_number "
                "AND b.project_id = tasks.project_id "
                "AND b.provider = 'trello' AND b.state = 'MATERIALIZED') "
                "WHERE reconciliation_origin IS NULL"
            )
            self._backfill_audit(conn)

    @staticmethod
    def _backfill_audit(conn: sqlite3.Connection) -> None:
        """Project legacy task history once, without changing lifecycle rows."""
        legacy = conn.execute(
            f"SELECT * FROM {TASKS_TABLE} t WHERE NOT EXISTS "
            f"(SELECT 1 FROM {AUDIT_EVENTS_TABLE} a WHERE a.task_id = t.task_id)"
        ).fetchall()
        names = {
            "READY": "WorkItemReady",
            "CLAIMED": "TaskClaimed",
            "PR_OPEN": "TaskPR_OPEN",
            "WAITING_HUMAN": "HumanApprovalRequired",
            "CHANGES_REQUESTED": "ChangesRequested",
            "DONE": "TaskCompleted",
        }
        for task in legacy:
            task_id = str(task["task_id"])
            # Each tuple is a stored fact, never provider prose or gate detail.
            facts: list[tuple[str, str, str, str, str, str, str | None, str | None]] = [
                (
                    "IssueMaterialized",
                    "task:" + task_id,
                    str(task["created_at"]),
                    task_id,
                    "task",
                    task_id,
                    None,
                    None,
                )
            ]
            transitions = conn.execute(
                f"SELECT * FROM {TRANSITIONS_TABLE} WHERE task_id = ? ORDER BY occurred_at, rowid",
                (task_id,),
            ).fetchall()
            if not transitions and task["status"] == "READY":
                facts.append(
                    (
                        "WorkItemReady",
                        "task:ready:" + task_id,
                        str(task["created_at"]),
                        task_id,
                        "task",
                        task_id,
                        None,
                        None,
                    )
                )
            for row in transitions:
                transition_id = str(row["transition_id"])
                facts.append(
                    (
                        names.get(str(row["to_status"]), "Task" + str(row["to_status"])),
                        "transition:" + transition_id,
                        str(row["occurred_at"]),
                        transition_id,
                        "task",
                        task_id,
                        None,
                        None,
                    )
                )
            runs = conn.execute(
                f"SELECT * FROM {AGENT_RUNS_TABLE} WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
            run_workspaces = {str(run["run_id"]): run["workspace_id"] for run in runs}
            for run in runs:
                run_id = str(run["run_id"])
                workspace_id = run["workspace_id"]
                facts.append(
                    (
                        "RunStarted",
                        "run:start:" + run_id,
                        str(run["started_at"] or run["created_at"]),
                        run_id,
                        "run",
                        run_id,
                        run_id,
                        workspace_id,
                    )
                )
                if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    finished_at = str(run["finished_at"] or run["created_at"])
                    facts.append(
                        (
                            "RunFinished",
                            "run:finish:" + run_id,
                            finished_at,
                            run_id,
                            "run",
                            run_id,
                            run_id,
                            workspace_id,
                        )
                    )
                    if run["status"] == "SUCCEEDED":
                        try:
                            gates = json.loads(str(run["gates"]))
                        except (TypeError, ValueError):
                            gates = []
                        failed = isinstance(gates, list) and any(
                            isinstance(gate, dict)
                            and gate.get("required", True)
                            and gate.get("status") != "PASSED"
                            for gate in gates
                        )
                        outcome = "failed" if failed else "passed"
                        facts.append(
                            (
                                "ValidationFailed" if failed else "ValidationPassed",
                                "validation:" + run_id + ":" + outcome,
                                finished_at,
                                run_id,
                                "run",
                                run_id,
                                run_id,
                                workspace_id,
                            )
                        )
            prs = conn.execute(
                f"SELECT p.* FROM {PULL_REQUESTS_TABLE} p JOIN {AGENT_RUNS_TABLE} r "
                "ON r.run_id = p.run_id WHERE r.task_id = ? ORDER BY p.opened_at, p.rowid",
                (task_id,),
            ).fetchall()
            for pr in prs:
                pr_id = str(pr["pull_request_id"])
                facts.append(
                    (
                        "PRCreated",
                        "pr:create:" + pr_id,
                        str(pr["opened_at"]),
                        str(pr["run_id"]),
                        "pull_request",
                        pr_id,
                        str(pr["run_id"]),
                        run_workspaces.get(str(pr["run_id"])),
                    )
                )
            versions: dict[tuple[str, str], int] = {}
            for sequence, fact in enumerate(
                sorted(facts, key=lambda item: (item[2], item[0] != "IssueMaterialized")),
                start=1,
            ):
                (
                    name,
                    event_key,
                    occurred_at,
                    cause,
                    aggregate,
                    aggregate_id,
                    linked_run_id,
                    linked_workspace_id,
                ) = fact
                identity = (aggregate, aggregate_id)
                version = versions.get(identity, 0) + 1
                versions[identity] = version
                conn.execute(
                    f"INSERT INTO {AUDIT_EVENTS_TABLE} "
                    "(event_key, correlation_id, event_seq, name, task_id, causation_id, "
                    "source_provider, source_repository, source_issue_number, "
                    "aggregate_type, aggregate_id, aggregate_version, occurred_at, "
                    "run_id, workspace_id, pull_request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_key,
                        task_id,
                        sequence,
                        name,
                        task_id,
                        cause,
                        task["source_provider"],
                        task["source_repository"],
                        task["source_issue_number"],
                        aggregate,
                        aggregate_id,
                        version,
                        occurred_at,
                        linked_run_id,
                        linked_workspace_id,
                        aggregate_id if aggregate == "pull_request" else None,
                    ),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            uri = Path(self._path).expanduser().resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(self._path)
        try:
            conn.row_factory = sqlite3.Row
            # Enforce the foreign keys from transitions/runs to their parents.
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def validate_read_only_schema(path: str, required_columns: dict[str, frozenset[str]]) -> None:
        """Fail closed when a status database is missing required tables or columns.

        This opens SQLite in URI read-only mode and performs metadata queries only;
        it deliberately does not call repository initialization or migrations.
        """
        database = Path(path).expanduser().resolve()
        if not database.is_file():
            raise ValueError("required database schema is absent or incompatible")
        uri = database.as_uri() + "?mode=ro"
        try:
            if any(re.fullmatch(r"[a-z_][a-z0-9_]*", name) is None for name in required_columns):
                raise ValueError("invalid internal schema identifier")
            conn = sqlite3.connect(uri, uri=True)
            try:
                conn.execute("PRAGMA query_only = ON")
                for table, expected in required_columns.items():
                    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    present = {str(row[1]) for row in rows}
                    if not expected.issubset(present):
                        raise ValueError("required database schema is absent or incompatible")
            finally:
                conn.close()
        except sqlite3.Error:
            raise ValueError("required database schema is absent or incompatible") from None


__all__ = ["SqliteRepository"]
