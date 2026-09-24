"""Persistence adapters.

Concrete storage lives here, behind the ports declared in
``factory.domain.ports``. The factory ships standard-library SQLite adapters for
local development; the orchestration layer never imports them.
"""

from factory.infrastructure.persistence.run_sqlite import SqliteRunRepository
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository

__all__ = ["SqliteRunRepository", "SqliteTaskRepository"]
