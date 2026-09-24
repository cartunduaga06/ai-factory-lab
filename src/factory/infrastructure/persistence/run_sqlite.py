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
from factory.domain.errors import DuplicateRunError
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
        :class:`~factory.domain.errors.DuplicateRunError`.
        """
        created_at = encode_datetime(datetime.now(UTC))
        try:
            with self._connect() as conn:
                if run.workspace is not None:
                    existing = conn.execute(
                        f"SELECT 1 FROM {WORKSPACES_TABLE} WHERE workspace_id = ?",
                        (run.workspace.workspace_id,),
                    ).fetchone()
                    if existing is None:
                        self._insert_workspace(conn, run.workspace)
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
            if _is_duplicate(exc):
                if run.status in _ACTIVE_STATUSES:
                    raise DuplicateRunError(run.run_id, task_id=run.task_id) from None
                raise DuplicateRunError(run.run_id) from None
            raise
        return run

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {AGENT_RUNS_TABLE} WHERE run_id = ?", (run_id,)
            ).fetchone()
            workspace = self._workspace_for(conn, row) if row is not None else None
        return _row_to_run(row, workspace) if row is not None else None

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
