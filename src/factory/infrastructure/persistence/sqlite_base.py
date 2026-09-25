"""Shared SQLite plumbing for the persistence adapters.

Both repositories open a fresh connection per operation and apply the same
schema, so the connection setup and idempotent initialization live here rather
than being duplicated. Keeping one connection per call makes a repository
trivially safe to share across threads and makes durability testable by simply
re-instantiating it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from factory.infrastructure.persistence.schema import (
    MIGRATION_STATEMENTS,
    SCHEMA_STATEMENTS,
)


class SqliteRepository:
    """Base class holding the file path, connection factory and schema setup."""

    def __init__(self, path: str) -> None:
        # ``:memory:`` is honoured; any other value is a filesystem path.
        self._path = path

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
        error. No migration drops or rewrites existing data.
        """
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            for statement in MIGRATION_STATEMENTS:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    # The column already exists (a fresh database, or a repeat
                    # initialization). Nothing to migrate; leave the data alone.
                    continue

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        # Enforce the foreign keys from transitions/runs to their parents.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


__all__ = ["SqliteRepository"]
