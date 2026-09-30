"""Fixed, bounded SQLite observations over operator registered local files."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from factory.domain.ports import DatabaseReadonlyInspector

MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_TABLES = 32
MAX_EVIDENCE_BYTES = 2048
TIMEOUT_SECONDS = 2.0


class SqliteReadonlyInspector(DatabaseReadonlyInspector):
    """Inspect a fixed set of SQLite facts without exposing paths or row data."""

    def __init__(
        self, targets: Mapping[str, str], *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._targets = dict(targets)
        self._monotonic = monotonic

    def target_path(self, target_id: str) -> str:
        return self._targets.get(target_id, "")

    def inspect(self, target_id: str) -> str:
        path = self._targets.get(target_id)
        if path is None:
            raise ValueError("database target is not registered")
        descriptor = -1
        deadline: float | None = None
        try:
            candidate = Path(path)
            if not candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
                raise ValueError("unsafe database target")
            current = Path("/")
            for part in candidate.parts[1:]:
                current = current / part
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ValueError("unsafe database target")
            if any(Path(path + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError("database has an active journal")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > MAX_DATABASE_BYTES
                or before.st_size < 100
            ):
                raise ValueError("unsafe database file")
            deadline = self._monotonic() + TIMEOUT_SECONDS
            uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True, timeout=0) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.set_progress_handler(
                    lambda: 1 if self._monotonic() >= deadline else 0, 100
                )
                connection.set_authorizer(_read_authorizer)
                version = connection.execute("SELECT sqlite_version()").fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()[0]
                if integrity != "ok":
                    raise ValueError("database integrity failed")
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' "
                    "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name LIMIT ?",
                    (MAX_TABLES + 1,),
                ).fetchall()
                if len(rows) > MAX_TABLES:
                    raise ValueError("database table limit exceeded")
                tables = []
                migration_version_sha256 = None
                for (name,) in rows:
                    if self._monotonic() >= deadline:
                        raise ValueError("database inspection timed out")
                    # SQLite identifier quoting is necessary even though names never
                    # originate in the Issue; bound them before constructing SQL.
                    if not isinstance(name, str) or len(name) > 128:
                        raise ValueError("database table name limit exceeded")
                    quoted = '"' + name.replace('"', '""') + '"'
                    count = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                    tables.append(
                        {"name_sha256": hashlib.sha256(name.encode()).hexdigest(), "rows": count}
                    )
                    if name == "alembic_version" and count == 1:
                        try:
                            version_row = connection.execute(
                                "SELECT version_num FROM alembic_version LIMIT 1"
                            ).fetchone()
                        except sqlite3.OperationalError:
                            version_row = None
                        if (
                            version_row is not None
                            and isinstance(version_row[0], str)
                            and 0 < len(version_row[0]) <= 128
                        ):
                            migration_version_sha256 = hashlib.sha256(
                                version_row[0].encode()
                            ).hexdigest()
                if self._monotonic() >= deadline:
                    raise ValueError("database inspection timed out")
                evidence = json.dumps(
                    {
                        "target_id": target_id,
                        "sqlite_version": version,
                        "integrity": "ok",
                        "user_version": user_version,
                        "schema_version": schema_version,
                        "migration_version_sha256": migration_version_sha256,
                        "tables": tables,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if len(evidence.encode()) > MAX_EVIDENCE_BYTES:
                    raise ValueError("database evidence limit exceeded")
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise ValueError("database changed during inspection")
            return evidence
        except sqlite3.OperationalError:
            if deadline is not None and self._monotonic() >= deadline:
                raise ValueError("database inspection timed out") from None
            raise ValueError("database inspection failed") from None
        except (OSError, sqlite3.Error):
            raise ValueError("database inspection failed") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _read_authorizer(
    action: int,
    arg1: str | None,
    _arg2: str | None,
    _db: str | None,
    _trigger: str | None,
) -> int:
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in {
        "integrity_check",
        "user_version",
        "schema_version",
    }:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
