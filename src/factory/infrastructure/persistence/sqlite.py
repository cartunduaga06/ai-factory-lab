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
from pathlib import Path

from factory.domain.enums import TaskStatus
from factory.domain.errors import DuplicateTaskError, TaskStateChangedError
from factory.domain.models import FactoryTask, TaskSource, TaskTransition
from factory.domain.ports import TaskRepository
from factory.infrastructure.persistence.schema import (
    SCHEMA_STATEMENTS,
    TASKS_TABLE,
    TRANSITIONS_TABLE,
)

# SQLite's own error class never escapes this module: callers see only the
# domain errors below, keeping the storage engine an implementation detail.


class SqliteTaskRepository(TaskRepository):
    """Durable task and transition storage backed by a SQLite file."""

    def __init__(self, path: str) -> None:
        # ``:memory:`` is honoured; any other value is a filesystem path.
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def __repr__(self) -> str:
        return f"SqliteTaskRepository(path={self._path!r})"

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        """Create the schema if it does not exist. Safe to call repeatedly."""
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        # Enforce the foreign key from transitions -> tasks.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- TaskRepository ----------------------------------------------------

    def save(self, task: FactoryTask) -> FactoryTask:
        source = task.source
        try:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {TASKS_TABLE} (
                        task_id, title, body, target_repository,
                        source_provider, source_repository, source_issue_number,
                        status, labels, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.title,
                        task.body,
                        task.target_repository,
                        source.provider if source else None,
                        source.repository_slug if source else None,
                        source.issue_number if source else None,
                        task.status.value,
                        _encode_labels(task.labels),
                        _encode_datetime(task.created_at),
                        _encode_datetime(task.updated_at),
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
                   SET title = ?, body = ?, target_repository = ?,
                       source_provider = ?, source_repository = ?,
                       source_issue_number = ?, status = ?, labels = ?, updated_at = ?
                 WHERE task_id = ?
                """,
                (
                    task.title,
                    task.body,
                    task.target_repository,
                    source.provider if source else None,
                    source.repository_slug if source else None,
                    source.issue_number if source else None,
                    task.status.value,
                    _encode_labels(task.labels),
                    _encode_datetime(task.updated_at),
                    task.task_id,
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
        now = datetime.now(UTC)
        timestamp = _encode_datetime(now)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {TASKS_TABLE}
                   SET status = ?, updated_at = ?
                 WHERE task_id = ? AND status = ?
                """,
                (target.value, timestamp, task_id, expected_from.value),
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


def _encode_datetime(value: datetime) -> str:
    return value.isoformat()


def _decode_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


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
        source=source,
        task_id=row["task_id"],
        body=row["body"],
        status=TaskStatus(row["status"]),
        labels=_decode_labels(row["labels"]),
        created_at=_decode_datetime(row["created_at"]),
        updated_at=_decode_datetime(row["updated_at"]),
    )


def _row_to_transition(row: sqlite3.Row) -> TaskTransition:
    return TaskTransition(
        task_id=row["task_id"],
        from_status=TaskStatus(row["from_status"]),
        to_status=TaskStatus(row["to_status"]),
        transition_id=row["transition_id"],
        occurred_at=_decode_datetime(row["occurred_at"]),
    )


__all__ = ["SqliteTaskRepository"]
