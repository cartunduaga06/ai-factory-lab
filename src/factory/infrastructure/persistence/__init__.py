"""Persistence adapters.

Concrete storage lives here, behind the port declared in
``factory.domain.ports``. Phase 2A ships a standard-library SQLite adapter for
local development; the orchestration layer never imports it.
"""

from factory.infrastructure.persistence.sqlite import SqliteTaskRepository

__all__ = ["SqliteTaskRepository"]
