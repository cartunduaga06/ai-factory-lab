"""Read-only projection of the durable, append-only execution trace."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from factory.infrastructure.persistence.schema import (
    AUDIT_EVENTS_TABLE,
    BACKLOG_LINKS_TABLE,
    TASKS_TABLE,
)
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Safe event view; opaque provider run identifiers are hashed for display."""

    sequence: int
    name: str
    correlation_id: str
    task_id: str
    run_id: str | None
    workspace_id: str | None
    pull_request_id: str | None
    causation_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    occurred_at: str
    source_provider: str | None
    source_issue_number: int | None


def _safe_id(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


class SqliteAuditEventStore(SqliteRepository):
    """Query the trace by task or structured Issue identity, never mutate it."""

    def for_task(self, task_id: str) -> tuple[AuditEvent, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {AUDIT_EVENTS_TABLE} WHERE task_id = ? ORDER BY event_seq",
                (task_id,),
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def for_sprint(self, sprint_id: str) -> tuple[AuditEvent, ...]:
        """Read sprint facts from the same append-only E1 event store."""
        return self.for_task(f"sprint:{sprint_id}")

    def for_issue(self, provider: str, repository: str, number: int) -> tuple[AuditEvent, ...]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT task_id FROM {TASKS_TABLE} WHERE source_provider = ? "
                "AND source_repository = ? AND source_issue_number = ?",
                (provider, repository, number),
            ).fetchone()
        return self.for_task(str(row["task_id"])) if row is not None else ()

    def for_work_item(self, provider: str, external_id: str) -> tuple[AuditEvent, ...]:
        """Follow a durable backlog link into the existing E1 task trace."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT repository_slug, issue_number FROM {BACKLOG_LINKS_TABLE} "
                "WHERE provider = ? AND external_id = ? AND state = 'MATERIALIZED'",
                (provider, external_id),
            ).fetchone()
        if row is None:
            return ()
        return self.for_issue("github", str(row["repository_slug"]), int(row["issue_number"]))

    @staticmethod
    def _event(row: object) -> AuditEvent:
        # sqlite3.Row is intentionally kept at this storage boundary.
        from sqlite3 import Row

        assert isinstance(row, Row)
        return AuditEvent(
            sequence=int(row["event_seq"]),
            name=str(row["name"]),
            correlation_id=str(row["correlation_id"]),
            task_id=str(row["task_id"]),
            run_id=_safe_id(row["run_id"]),
            workspace_id=row["workspace_id"],
            pull_request_id=row["pull_request_id"],
            causation_id=_safe_id(row["causation_id"]) or "",
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=(
                _safe_id(row["aggregate_id"]) or ""
                if row["aggregate_type"] == "run"
                else str(row["aggregate_id"])
            ),
            aggregate_version=int(row["aggregate_version"]),
            occurred_at=str(row["occurred_at"]),
            source_provider=row["source_provider"],
            source_issue_number=row["source_issue_number"],
        )


__all__ = ["AuditEvent", "SqliteAuditEventStore"]
