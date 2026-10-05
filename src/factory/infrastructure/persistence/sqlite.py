"""SQLite implementation of the :class:`~factory.domain.ports.TaskRepository` port.

Standard-library ``sqlite3`` only — no ORM. The orchestration layer never sees
these details; it depends on the port.

Two contracts matter most here:

* **Idempotent schema.** :meth:`SqliteTaskRepository.initialize` can be called
  repeatedly and on an existing database.
* **Atomic transitions.** :meth:`apply_transition` loads the task, validates the
  transition, updates the status and appends history inside a single
  transaction. If any step raises, the transaction is rolled back, so an invalid
  transition leaves both the status and the history untouched.

A new connection is opened per operation and closed immediately. That keeps the
repository trivially safe to share across threads and makes durability testable
by simply re-instantiating the repository.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from factory.domain.enums import TaskKind, TaskStatus
from factory.domain.errors import DuplicateTaskError, InvalidTransitionError, TaskStateChangedError
from factory.domain.models import FactoryTask, TaskSource, TaskTransition
from factory.domain.ports import TaskRepository
from factory.domain.task_lifecycle import can_transition
from factory.infrastructure.persistence.codec import decode_datetime, encode_datetime
from factory.infrastructure.persistence.schema import (
    AGENT_RUNS_TABLE,
    BACKLOG_LINKS_TABLE,
    QA_REWORK_TABLE,
    TASKS_TABLE,
    TRANSITIONS_TABLE,
)
from factory.infrastructure.persistence.sqlite_base import SqliteRepository

# SQLite's own error class never escapes this module: callers see only the
# domain errors below, keeping the storage engine an implementation detail.


class SqliteTaskRepository(SqliteRepository, TaskRepository):
    """Durable task and transition storage backed by a SQLite file."""

    def __init__(self, path: str, *, max_active_claims: int = 1, read_only: bool = False) -> None:
        super().__init__(path, read_only=read_only)
        if not 1 <= max_active_claims <= 2:
            raise ValueError("max_active_claims must be 1 or 2")
        self._max_active_claims = max_active_claims

    def __repr__(self) -> str:
        return f"SqliteTaskRepository(path={self._path!r})"

    def request_rework(self, task_id: str, run_id: str, feedback: str) -> FactoryTask:
        now = encode_datetime(datetime.now(UTC))
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TASKS_TABLE} SET status = ?, updated_at = ? "
                "WHERE task_id = ? AND status = ?",
                (TaskStatus.CHANGES_REQUESTED.value, now, task_id, TaskStatus.WAITING_HUMAN.value),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    f"SELECT status FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                raise TaskStateChangedError(
                    task_id, TaskStatus.WAITING_HUMAN, TaskStatus(row["status"])
                )
            conn.execute(
                f"INSERT INTO {QA_REWORK_TABLE} "
                "(request_id, task_id, reviewed_run_id, feedback, requested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), task_id, run_id, feedback, now),
            )
            conn.execute(
                f"INSERT INTO {TRANSITIONS_TABLE} "
                "(transition_id, task_id, from_status, to_status, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    task_id,
                    TaskStatus.WAITING_HUMAN.value,
                    TaskStatus.CHANGES_REQUESTED.value,
                    now,
                ),
            )
            row = conn.execute(
                f"SELECT * FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return _row_to_task(row)

    def latest_rework_feedback(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT feedback FROM {QA_REWORK_TABLE} WHERE task_id = ? "
                "ORDER BY requested_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return str(row["feedback"]) if row is not None else None

    # -- TaskRepository ----------------------------------------------------

    def save(self, task: FactoryTask) -> FactoryTask:
        source = task.source
        try:
            with self._connect() as conn:
                link = (
                    conn.execute(
                        f"SELECT provider, external_id FROM {BACKLOG_LINKS_TABLE} "
                        "WHERE repository_slug = ? AND issue_number = ? AND project_id = ? "
                        "AND state = 'MATERIALIZED'",
                        (source.repository_slug, source.issue_number, task.project_id),
                    ).fetchone()
                    if source is not None
                    else None
                )
                conn.execute(
                    f"""
                    INSERT INTO {TASKS_TABLE} (
                        task_id, title, body, target_repository, project_id,
                        source_provider, source_repository, source_issue_number,
                        status, labels, kind, blocked_reason, created_at, updated_at,
                        reconciliation_origin, expected_work_item_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.title,
                        task.body,
                        task.target_repository,
                        task.project_id,
                        source.provider if source else None,
                        source.repository_slug if source else None,
                        source.issue_number if source else None,
                        task.status.value,
                        _encode_labels(task.labels),
                        task.kind.value,
                        task.blocked_reason,
                        encode_datetime(task.created_at),
                        encode_datetime(task.updated_at),
                        str(link["provider"]) if link is not None else "github-direct",
                        str(link["external_id"]) if link is not None else None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if source is not None and _is_unique_violation(exc):
                raise DuplicateTaskError(source) from None
            raise
        return task

    def update(self, task: FactoryTask) -> FactoryTask:
        source = task.source
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {TASKS_TABLE}
                   SET title = ?, body = ?, target_repository = ?, project_id = ?,
                       source_provider = ?, source_repository = ?,
                       source_issue_number = ?, status = ?, labels = ?, kind = ?,
                       blocked_reason = ?, updated_at = ?
                 WHERE task_id = ? AND kind = ? AND project_id = ?
                   AND target_repository = ?
                """,
                (
                    task.title,
                    task.body,
                    task.target_repository,
                    task.project_id,
                    source.provider if source else None,
                    source.repository_slug if source else None,
                    source.issue_number if source else None,
                    task.status.value,
                    _encode_labels(task.labels),
                    task.kind.value,
                    task.blocked_reason,
                    encode_datetime(task.updated_at),
                    task.task_id,
                    task.kind.value,
                    task.project_id,
                    task.target_repository,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(task.task_id)
        return task

    def get(self, task_id: str) -> FactoryTask | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def find_by_source(self, source: TaskSource) -> FactoryTask | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {TASKS_TABLE}
                 WHERE source_provider = ?
                   AND source_repository = ?
                   AND source_issue_number = ?
                """,
                (source.provider, source.repository_slug, source.issue_number),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def list(self, status: TaskStatus | None = None) -> Sequence[FactoryTask]:
        query = f"SELECT * FROM {TASKS_TABLE}"
        params: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status.value,)
        query += " ORDER BY created_at ASC, task_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_task(row) for row in rows]

    def apply_transition(
        self, task_id: str, expected_from: TaskStatus, target: TaskStatus
    ) -> FactoryTask:
        """Atomically apply ``expected_from -> target`` and record history.

        The compare-and-swap is a single conditional ``UPDATE`` whose predicate
        includes ``expected_from``. SQLite serializes the writes and re-evaluates
        the predicate against the committed state, so two writers sharing the same
        ``expected_from`` cannot both succeed — the loser matches zero rows and is
        refused with :class:`TaskStateChangedError`. The status update and the
        history insert share one transaction, so a failure in either leaves both
        untouched.
        """
        if not can_transition(expected_from, target):
            raise InvalidTransitionError(expected_from, target)

        now = datetime.now(UTC)
        timestamp = encode_datetime(now)
        with self._connect() as conn:
            # Count distinct task identities, including orphaned active runs.
            # The count and transition share SQLite's serialized write transaction.
            idle_guard = (
                "AND (SELECT COUNT(*) FROM ("
                f"SELECT busy.task_id FROM {TASKS_TABLE} AS busy "
                "WHERE busy.task_id != ? AND busy.status IN "
                "('CLAIMED', 'RUNNING', 'VALIDATING', 'PR_OPEN') "
                "UNION "
                f"SELECT active.task_id FROM {AGENT_RUNS_TABLE} AS active "
                "WHERE active.task_id != ? AND active.status IN ('PENDING', 'RUNNING')"
                ")) < ?"
                if target is TaskStatus.CLAIMED
                else ""
            )
            cursor = conn.execute(
                f"""
                UPDATE {TASKS_TABLE}
                   SET status = ?, updated_at = ?
                 WHERE task_id = ? AND status = ? {idle_guard}
                """,
                (
                    target.value,
                    timestamp,
                    task_id,
                    expected_from.value,
                    *(
                        (task_id, task_id, self._max_active_claims)
                        if target is TaskStatus.CLAIMED
                        else ()
                    ),
                ),
            )
            if cursor.rowcount == 0:
                # The guarded write matched nothing: the task is either gone or its
                # stored status moved on. Re-read inside the transaction to report
                # which, and leave state and history untouched.
                row = conn.execute(
                    f"SELECT status FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                raise TaskStateChangedError(task_id, expected_from, TaskStatus(row["status"]))

            conn.execute(
                f"""
                INSERT INTO {TRANSITIONS_TABLE} (
                    transition_id, task_id, from_status, to_status, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), task_id, expected_from.value, target.value, timestamp),
            )

            updated = conn.execute(
                f"SELECT * FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert updated is not None  # the guarded UPDATE above proved the row exists
        return _row_to_task(updated)

    def authorize_terminal_timeout_recovery(self, task_id: str, run_id: str) -> FactoryTask:
        """Exceptional operator-approved timeout recovery, atomically audited."""
        return self._authorize_terminal_recovery(
            task_id, run_id, marker="terminal-timeout-recovery:" + run_id
        )

    def authorize_terminal_worker_recovery(self, task_id: str, run_id: str) -> FactoryTask:
        """Exceptional operator-approved clean worker recovery, atomically audited."""
        return self._authorize_terminal_recovery(
            task_id, run_id, marker="terminal-worker-recovery:" + run_id
        )

    def authorize_orphaned_codex_recovery(self, task_id: str, run_id: str) -> FactoryTask:
        """Atomically fail the exact latest orphan and block its task for retry."""
        timestamp = encode_datetime(datetime.now(UTC))
        marker = "orphaned-codex-recovery:" + run_id
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT r.run_id FROM {AGENT_RUNS_TABLE} r
                    JOIN {TASKS_TABLE} t ON t.task_id = r.task_id
                    WHERE t.task_id = ? AND t.status = 'RUNNING' AND t.kind = 'CODE'
                      AND r.project_id = t.project_id
                      AND r.workspace_id IS NOT NULL
                      AND r.run_id = ? AND r.status = 'RUNNING' AND r.adapter = 'CODEX'
                      AND r.rowid = (
                        SELECT x.rowid FROM {AGENT_RUNS_TABLE} x
                        WHERE x.task_id = t.task_id
                        ORDER BY x.created_at DESC, x.rowid DESC LIMIT 1
                      )
                      AND NOT EXISTS (SELECT 1 FROM pull_requests p WHERE p.task_id = t.task_id)
                      """,
                (task_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("orphan recovery identity guard rejected")
            conn.execute(
                f"UPDATE {AGENT_RUNS_TABLE} SET status='FAILED', summary=?, "
                "finished_at=? WHERE run_id=? AND status='RUNNING'",
                (marker, timestamp, run_id),
            )
            cursor = conn.execute(
                f"UPDATE {TASKS_TABLE} SET status='BLOCKED', blocked_reason=?, "
                "updated_at=? WHERE task_id=? AND status='RUNNING'",
                (marker, timestamp, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("orphan recovery task state changed")
            transition_id = str(uuid.uuid4())
            conn.execute(
                f"INSERT INTO {TRANSITIONS_TABLE} "
                "(transition_id,task_id,from_status,to_status,occurred_at) "
                "VALUES (?,?, 'RUNNING','BLOCKED',?)",
                (transition_id, task_id, timestamp),
            )
            # The transition trigger records the immutable E1 projection in this transaction.
            updated = conn.execute(
                f"SELECT * FROM {TASKS_TABLE} WHERE task_id=?", (task_id,)
            ).fetchone()
        assert updated is not None
        return _row_to_task(updated)

    def _authorize_terminal_recovery(
        self, task_id: str, run_id: str, *, marker: str
    ) -> FactoryTask:
        """Atomically record one exceptional FAILED -> BLOCKED recovery."""
        timestamp = encode_datetime(datetime.now(UTC))
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {TASKS_TABLE}
                   SET status = ?, blocked_reason = ?, updated_at = ?
                 WHERE task_id = ? AND status = 'FAILED' AND kind = 'CODE'
                   AND EXISTS (
                     SELECT 1 FROM {AGENT_RUNS_TABLE} r
                     WHERE r.run_id = ? AND r.task_id = {TASKS_TABLE}.task_id
                       AND r.status = 'FAILED' AND r.workspace_id IS NOT NULL
                       AND r.rowid = (
                         SELECT x.rowid FROM {AGENT_RUNS_TABLE} x
                         WHERE x.task_id = {TASKS_TABLE}.task_id
                         ORDER BY x.created_at DESC, x.run_id DESC LIMIT 1
                       )
                   )
                   AND NOT EXISTS (
                     SELECT 1 FROM {AGENT_RUNS_TABLE} active
                     WHERE active.status IN ('PENDING', 'RUNNING')
                   )
                   AND NOT EXISTS (
                     SELECT 1 FROM pull_requests pr
                     WHERE pr.task_id = {TASKS_TABLE}.task_id
                   )
                """,
                (TaskStatus.BLOCKED.value, marker, timestamp, task_id, run_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    f"SELECT status FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                if row["status"] != TaskStatus.FAILED.value:
                    raise TaskStateChangedError(
                        task_id, TaskStatus.FAILED, TaskStatus(row["status"])
                    )
                raise ValueError("terminal recovery evidence or concurrency guard rejected")
            # audit_transition adds the immutable E1 fact in the same transaction.
            conn.execute(
                f"""INSERT INTO {TRANSITIONS_TABLE}
                    (transition_id, task_id, from_status, to_status, occurred_at)
                    VALUES (?, ?, 'FAILED', 'BLOCKED', ?)""",
                (str(uuid.uuid4()), task_id, timestamp),
            )
            updated = conn.execute(
                f"SELECT * FROM {TASKS_TABLE} WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert updated is not None
        return _row_to_task(updated)

    def history(self, task_id: str) -> Sequence[TaskTransition]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {TRANSITIONS_TABLE}
                 WHERE task_id = ?
                 ORDER BY occurred_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [_row_to_transition(row) for row in rows]


# -- serialization helpers -------------------------------------------------


def _encode_labels(labels: Sequence[str]) -> str:
    return json.dumps(list(labels))


def _decode_labels(raw: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded)


def _is_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    return "UNIQUE" in str(exc).upper()


def _row_to_task(row: sqlite3.Row) -> FactoryTask:
    provider = row["source_provider"]
    source = (
        TaskSource(
            provider=provider,
            repository_slug=row["source_repository"],
            issue_number=row["source_issue_number"],
        )
        if provider is not None
        else None
    )
    return FactoryTask(
        title=row["title"],
        target_repository=row["target_repository"],
        project_id=row["project_id"],
        source=source,
        task_id=row["task_id"],
        body=row["body"],
        status=TaskStatus(row["status"]),
        labels=_decode_labels(row["labels"]),
        kind=TaskKind(row["kind"]),
        blocked_reason=row["blocked_reason"],
        created_at=decode_datetime(row["created_at"]),
        updated_at=decode_datetime(row["updated_at"]),
    )


def _row_to_transition(row: sqlite3.Row) -> TaskTransition:
    return TaskTransition(
        task_id=row["task_id"],
        from_status=TaskStatus(row["from_status"]),
        to_status=TaskStatus(row["to_status"]),
        transition_id=row["transition_id"],
        occurred_at=decode_datetime(row["occurred_at"]),
    )


__all__ = ["SqliteTaskRepository"]
