"""SQLite implementation of controlled operational memory."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime

from factory.domain.memory import MemoryEvidence, MemoryRecord, MemoryRepository, MemoryStatus
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqliteMemoryRepository(SqliteRepository, MemoryRepository):
    """Persist versioned memory and auditable lifecycle events in the Factory DB."""

    def initialize(self) -> None:
        super().initialize()
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
                status TEXT NOT NULL CHECK(status IN (
                    'CANDIDATE','VALIDATED','USED','APPROVED',
                    'SUPERSEDED','EXPIRED','ROLLED_BACK')),
                evidence TEXT NOT NULL, approved_by TEXT, expires_at TEXT, supersedes TEXT,
                created_at TEXT NOT NULL, UNIQUE(kind, title, version))"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_events (
                event_id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, actor TEXT,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(memory_id))"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_memories_status_repo ON memories(status)")

    def save_candidate(self, record: MemoryRecord) -> MemoryRecord:
        if record.status is not MemoryStatus.CANDIDATE:
            raise ValueError("new memory must be a candidate")
        evidence = json.dumps([_evidence_to_dict(item) for item in record.evidence], sort_keys=True)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM memories WHERE memory_id=?", (record.memory_id,)
            ).fetchone()
            if existing is not None:
                restored = _record(existing)
                if restored != record:
                    raise ValueError("memory identity conflict")
                return restored
            conn.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.memory_id,
                    record.kind,
                    record.title,
                    record.content,
                    record.version,
                    record.status.value,
                    evidence,
                    None,
                    _iso(record.expires_at),
                    record.supersedes,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO memory_events(memory_id,to_status,occurred_at) VALUES(?,?,?)",
                (record.memory_id, record.status.value, datetime.now(UTC).isoformat()),
            )
        return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        return _record(row) if row is not None else None

    def transition(
        self,
        memory_id: str,
        expected: MemoryStatus,
        target: MemoryStatus,
        *,
        actor: str | None = None,
    ) -> MemoryRecord:
        allowed = {
            MemoryStatus.CANDIDATE: {
                MemoryStatus.VALIDATED,
                MemoryStatus.APPROVED,
                MemoryStatus.ROLLED_BACK,
            },
            MemoryStatus.VALIDATED: {
                MemoryStatus.USED,
                MemoryStatus.APPROVED,
                MemoryStatus.ROLLED_BACK,
            },
            MemoryStatus.USED: {MemoryStatus.APPROVED, MemoryStatus.ROLLED_BACK},
            MemoryStatus.APPROVED: {
                MemoryStatus.SUPERSEDED,
                MemoryStatus.EXPIRED,
                MemoryStatus.ROLLED_BACK,
            },
        }
        if target not in allowed.get(expected, set()):
            raise ValueError("invalid memory lifecycle transition")
        if target is MemoryStatus.APPROVED and not actor:
            raise ValueError("approval requires an explicit human actor")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            current = _record(row)
            if current.status is not expected:
                raise ValueError("memory status changed")
            conn.execute(
                "UPDATE memories SET status=?, approved_by=? WHERE memory_id=? AND status=?",
                (
                    target.value,
                    actor if target is MemoryStatus.APPROVED else current.approved_by,
                    memory_id,
                    expected.value,
                ),
            )
            conn.execute(
                "INSERT INTO memory_events(memory_id,from_status,to_status,actor,"
                "occurred_at) VALUES(?,?,?,?,?)",
                (memory_id, expected.value, target.value, actor, datetime.now(UTC).isoformat()),
            )
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        return _record(row)

    def search_approved(
        self, query: str, *, repository: str, limit: int
    ) -> tuple[MemoryRecord, ...]:
        if not 1 <= limit <= 10:
            raise ValueError("invalid retrieval limit")
        terms = {term.casefold() for term in re.findall(r"[a-zA-Z0-9_-]{3,}", query)}
        if not terms:
            return ()
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE status='APPROVED' "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now,),
            ).fetchall()
        candidates: list[tuple[int, str, MemoryRecord]] = []
        for row in rows:
            record = _record(row)
            if not any(item.repository == repository for item in record.evidence):
                continue
            words = set(
                re.findall(r"[a-zA-Z0-9_-]{3,}", f"{record.title} {record.content}".casefold())
            )
            score = len(terms & words)
            if score:
                candidates.append((score, record.memory_id, record))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in candidates[:limit])


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _evidence_to_dict(item: MemoryEvidence) -> dict[str, object]:
    return {
        "repository": item.repository,
        "reference": item.reference,
        "task_id": item.task_id,
        "run_id": item.run_id,
        "gates": list(item.gates),
        "observed_at": _iso(item.observed_at),
    }


def _record(row: sqlite3.Row) -> MemoryRecord:
    evidence = json.loads(row["evidence"])
    return MemoryRecord(
        memory_id=row["memory_id"],
        kind=row["kind"],
        title=row["title"],
        content=row["content"],
        version=row["version"],
        status=MemoryStatus(row["status"]),
        evidence=tuple(
            MemoryEvidence(
                repository=item["repository"],
                reference=item["reference"],
                task_id=item["task_id"],
                run_id=item["run_id"],
                gates=tuple(item["gates"]),
                observed_at=datetime.fromisoformat(item["observed_at"])
                if item["observed_at"]
                else None,
            )
            for item in evidence
        ),
        approved_by=row["approved_by"],
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        supersedes=row["supersedes"],
    )
