"""SQLite event outbox for transitions and deterministic stall observations."""

from __future__ import annotations

from datetime import UTC, datetime

from factory.domain.models import StatusSnapshot
from factory.infrastructure.persistence.schema import STATUS_EVENTS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqliteStatusEventStore(SqliteRepository):
    """Keep events until a channel confirms delivery."""

    def pending(self) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT transition_id, task_id FROM {STATUS_EVENTS_TABLE} "
                "WHERE delivered_at IS NULL ORDER BY occurred_at, rowid LIMIT 100"
            ).fetchall()
        return [(row["transition_id"], row["task_id"]) for row in rows]

    def mark_delivered(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {STATUS_EVENTS_TABLE} SET delivered_at = ? "
                "WHERE transition_id = ? AND delivered_at IS NULL",
                (datetime.now(UTC).isoformat(), event_id),
            )

    def observe(self, snapshot: StatusSnapshot) -> None:
        if snapshot.task_id is None or snapshot.run_id is None:
            return
        if snapshot.phase == "STALLED":
            event_id = f"stall:{snapshot.run_id}:{snapshot.last_heartbeat or snapshot.started_at}"
        elif snapshot.phase == "RUNNING" and snapshot.last_heartbeat is not None:
            event_id = f"recovered:{snapshot.run_id}:{snapshot.last_heartbeat}"
            with self._connect() as conn:
                stalled = conn.execute(
                    f"SELECT 1 FROM {STATUS_EVENTS_TABLE} "
                    "WHERE task_id = ? AND phase = 'STALLED' "
                    "AND transition_id LIKE ? LIMIT 1",
                    (snapshot.task_id, f"stall:{snapshot.run_id}:%"),
                ).fetchone()
            if stalled is None:
                return
        else:
            return
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO {STATUS_EVENTS_TABLE} "
                "(transition_id, task_id, phase, occurred_at) VALUES (?, ?, ?, ?)",
                (event_id, snapshot.task_id, snapshot.phase, datetime.now(UTC).isoformat()),
            )
