"""SQLite implementation of the :class:`~factory.domain.ports.RunRepository` port.

Standard-library ``sqlite3`` only — no ORM. Dispatch never sees these details; it
depends on the port.

Two contracts matter most here:

* **Atomic run persistence.** :meth:`SqliteRunRepository.save_run` writes the
  run's workspace (when one is attached) and the run itself inside a single
  transaction, so a run is never stored without the workspace it operates in.
* **One active run per task.** A partial unique index
  (``uq_agent_runs_active_task``) allows at most one non-terminal run per task.
  That is the storage-level guard behind dispatch idempotency: two concurrent
  dispatchers cannot both persist an active run, even if both pass the
  application-level check.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus
from factory.domain.errors import DuplicateRunError, FactoryError, PersistenceError
from factory.domain.models import AgentRun, QualityGate, Workspace
from factory.domain.ports import RunRepository
from factory.infrastructure.persistence.codec import decode_datetime, encode_datetime
from factory.infrastructure.persistence.schema import AGENT_RUNS_TABLE, WORKSPACES_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository

#: Statuses excluded from the active-run index, mirrored from the schema.
_ACTIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)


class SqliteRunRepository(SqliteRepository, RunRepository):
    """Durable workspace and agent-run storage backed by a SQLite file."""

    def __repr__(self) -> str:
        return f"SqliteRunRepository(path={self._path!r})"

    # -- RunRepository -----------------------------------------------------

    def save_workspace(self, workspace: Workspace) -> Workspace:
        try:
            with self._connect() as conn:
                self._insert_workspace(conn, workspace)
        except sqlite3.IntegrityError as exc:
            if _is_duplicate(exc):
                raise DuplicateRunError(workspace.workspace_id) from None
            raise
        return workspace

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {WORKSPACES_TABLE} WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        return _row_to_workspace(row) if row is not None else None

    def save_run(self, run: AgentRun) -> AgentRun:
        """Persist ``run`` and, when attached, its workspace, atomically.

        The workspace insert and the run insert share one transaction, so a
        failure in either leaves both untouched. A task that already has an
        active run is refused by the partial unique index and surfaces as
        :class:`~factory.domain.errors.DuplicateRunError`; a workspace already
        claimed by another run is refused by the one-workspace-per-run index and
        surfaces the same way, so no two runs share a working tree.
        """
        created_at = encode_datetime(datetime.now(UTC))
        try:
            with self._connect() as conn:
                if run.workspace is not None:
                    self._ensure_workspace(conn, run.workspace)
                conn.execute(
                    f"""
                    INSERT INTO {AGENT_RUNS_TABLE} (
                        run_id, task_id, adapter, status, workspace_id,
                        summary, started_at, finished_at, gates, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.task_id,
                        run.adapter.value,
                        run.status.value,
                        run.workspace.workspace_id if run.workspace else None,
                        run.summary,
                        encode_datetime(run.started_at) if run.started_at else None,
                        encode_datetime(run.finished_at) if run.finished_at else None,
                        _encode_gates(run.gates),
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise self._duplicate_error(exc, run) from None
        return run

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {AGENT_RUNS_TABLE} WHERE run_id = ?", (run_id,)
            ).fetchone()
            workspace = self._workspace_for(conn, row) if row is not None else None
        return _row_to_run(row, workspace) if row is not None else None

    def update_run(self, run: AgentRun) -> AgentRun:
        """Update an already-stored run. Never inserts, never re-associates.

        The row is matched by ``run_id``; a missing row is refused with
        ``KeyError`` rather than upserted, so this operation can never create a
        second run or bypass the one-active-run invariant. The run's identity is
        immutable: ``task_id`` and ``workspace_id`` are fixed once the run is
        saved, so a caller cannot re-point a run at another task or another
        workspace. ``workspace_id`` is deliberately absent from the ``UPDATE``
        below, so the isolation invariant holds even if a caller bypasses the
        check.
        """
        with self._connect() as conn:
            existing = conn.execute(
                f"""
                SELECT task_id, workspace_id, created_at
                  FROM {AGENT_RUNS_TABLE} WHERE run_id = ?
                """,
                (run.run_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(run.run_id)
            if existing["task_id"] != run.task_id:
                raise PersistenceError(f"run {run.run_id} belongs to another task")

            new_workspace_id = run.workspace.workspace_id if run.workspace is not None else None
            if new_workspace_id != existing["workspace_id"]:
                # Sanitized: the message names only the run id. Workspace paths,
                # branches and storage details must not escape this boundary.
                raise PersistenceError(f"run {run.run_id} cannot change its workspace")

            conn.execute(
                f"""
                UPDATE {AGENT_RUNS_TABLE}
                   SET status = ?, summary = ?, started_at = ?, finished_at = ?,
                       gates = ?
                 WHERE run_id = ?
                """,
                (
                    run.status.value,
                    run.summary,
                    encode_datetime(run.started_at) if run.started_at else None,
                    encode_datetime(run.finished_at) if run.finished_at else None,
                    _encode_gates(run.gates),
                    run.run_id,
                ),
            )
        return run

    def list_runs(self, task_id: str | None = None) -> Sequence[AgentRun]:
        query = f"SELECT * FROM {AGENT_RUNS_TABLE}"
        params: tuple[object, ...] = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY created_at ASC, run_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [_row_to_run(row, self._workspace_for(conn, row)) for row in rows]

    def find_active_run(self, task_id: str) -> AgentRun | None:
        placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {AGENT_RUNS_TABLE}
                 WHERE task_id = ? AND status IN ({placeholders})
                 ORDER BY created_at ASC, run_id ASC
                 LIMIT 1
                """,
                (task_id, *(status.value for status in _ACTIVE_STATUSES)),
            ).fetchone()
            workspace = self._workspace_for(conn, row) if row is not None else None
        return _row_to_run(row, workspace) if row is not None else None

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _insert_workspace(conn: sqlite3.Connection, workspace: Workspace) -> None:
        conn.execute(
            f"""
            INSERT INTO {WORKSPACES_TABLE} (
                workspace_id, repository_slug, branch, path, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                workspace.workspace_id,
                workspace.repository_slug,
                workspace.branch,
                workspace.path,
                encode_datetime(workspace.created_at),
            ),
        )

    @classmethod
    def _ensure_workspace(cls, conn: sqlite3.Connection, workspace: Workspace) -> None:
        """Insert the workspace if it is not already stored, else leave it.

        A run's workspace is written once at dispatch. On a later update the same
        workspace is re-attached, and re-inserting its row would violate the
        primary key, so presence is checked first. The existing row is not
        modified: a workspace is immutable in practice.
        """
        existing = conn.execute(
            f"SELECT 1 FROM {WORKSPACES_TABLE} WHERE workspace_id = ?",
            (workspace.workspace_id,),
        ).fetchone()
        if existing is None:
            cls._insert_workspace(conn, workspace)

    @staticmethod
    def _duplicate_error(exc: sqlite3.IntegrityError, run: AgentRun) -> FactoryError:
        """Map a SQLite integrity failure to a sanitized factory error.

        The raw SQLite message names table columns and can include stored values,
        so it is never surfaced: only the discriminating outcome crosses the
        boundary. A workspace-reuse refusal and an active-run clash are different
        facts and carry different signals rather than collapsing into one.

        Returns the error to raise; the caller does ``raise ... from None`` so no
        raw SQLite text is retained as ``__cause__``/``__context__``.
        """
        if not _is_duplicate(exc):
            # An unexpected integrity failure (for example a missing task via the
            # foreign key). Its text may embed stored values, so it is normalized.
            return PersistenceError("run could not be persisted")
        if "workspace" in str(exc).lower():
            workspace_id = run.workspace.workspace_id if run.workspace is not None else None
            return DuplicateRunError(run.run_id, workspace_id=workspace_id)
        if run.status in _ACTIVE_STATUSES:
            return DuplicateRunError(run.run_id, task_id=run.task_id)
        return DuplicateRunError(run.run_id)

    @staticmethod
    def _workspace_for(conn: sqlite3.Connection, row: sqlite3.Row) -> Workspace | None:
        workspace_id = row["workspace_id"]
        if workspace_id is None:
            return None
        workspace_row = conn.execute(
            f"SELECT * FROM {WORKSPACES_TABLE} WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return _row_to_workspace(workspace_row) if workspace_row is not None else None


# -- serialization helpers -------------------------------------------------


def _encode_gates(gates: Sequence[QualityGate]) -> str:
    return json.dumps(
        [
            {
                "name": gate.name,
                "status": gate.status.value,
                "detail": gate.detail,
                "required": gate.required,
            }
            for gate in gates
        ]
    )


def _decode_gates(raw: str) -> tuple[QualityGate, ...]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    gates: list[QualityGate] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        gates.append(
            QualityGate(
                name=str(item["name"]),
                status=QualityGateStatus(str(item.get("status", QualityGateStatus.PENDING))),
                detail=item.get("detail"),
                required=bool(item.get("required", True)),
            )
        )
    return tuple(gates)


def _is_duplicate(exc: sqlite3.IntegrityError) -> bool:
    return "UNIQUE" in str(exc).upper()


def _row_to_workspace(row: sqlite3.Row) -> Workspace:
    return Workspace(
        workspace_id=row["workspace_id"],
        repository_slug=row["repository_slug"],
        branch=row["branch"],
        path=row["path"],
        created_at=decode_datetime(row["created_at"]),
    )


def _row_to_run(row: sqlite3.Row, workspace: Workspace | None) -> AgentRun:
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    return AgentRun(
        task_id=row["task_id"],
        adapter=AgentKind(row["adapter"]),
        run_id=row["run_id"],
        status=RunStatus(row["status"]),
        workspace=workspace,
        summary=row["summary"],
        started_at=decode_datetime(started_at) if started_at else None,
        finished_at=decode_datetime(finished_at) if finished_at else None,
        gates=_decode_gates(row["gates"]),
    )


__all__ = ["SqliteRunRepository"]
