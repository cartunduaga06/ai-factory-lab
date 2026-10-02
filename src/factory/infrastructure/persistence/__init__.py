"""Persistence adapters.

Concrete storage lives here, behind the ports declared in
``factory.domain.ports``. The factory ships standard-library SQLite adapters for
local development; the orchestration layer never imports them.
"""

from factory.infrastructure.persistence.backlog_sqlite import SqliteBacklogLinkRepository
from factory.infrastructure.persistence.pr_sqlite import SqlitePullRequestRepository
from factory.infrastructure.persistence.revalidation_sqlite import (
    SqlitePostRebaseRevalidationRepository,
)
from factory.infrastructure.persistence.run_sqlite import SqliteRunRepository
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository

__all__ = [
    "SqliteBacklogLinkRepository",
    "SqlitePullRequestRepository",
    "SqlitePostRebaseRevalidationRepository",
    "SqliteRunRepository",
    "SqliteTaskRepository",
]
