"""Pure-domain typed model.

This package holds data structures only: no network, database or filesystem
access. Keeping the domain free of I/O is what allows the orchestration layer
to stay independent from any specific agent engine.
"""

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RepositoryRole,
    RunStatus,
    TaskStatus,
)
from factory.domain.errors import (
    AgentDispatchError,
    DispatchConflictError,
    DispatchError,
    DuplicateRunError,
    DuplicateTaskError,
    FactoryError,
    PersistenceError,
    TaskNotReadyError,
    TaskSourceError,
    TaskStateChangedError,
)
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    Repository,
    TaskSource,
    TaskTransition,
    Workspace,
)
from factory.domain.ports import IssueSource, RunRepository, TaskRepository

__all__ = [
    "AgentAdapter",
    "AgentDispatchError",
    "AgentKind",
    "AgentRun",
    "DispatchConflictError",
    "DispatchError",
    "DuplicateRunError",
    "DuplicateTaskError",
    "FactoryError",
    "FactoryTask",
    "IssueSource",
    "PersistenceError",
    "PullRequest",
    "QualityGate",
    "QualityGateStatus",
    "Repository",
    "RepositoryRole",
    "RunRepository",
    "RunStatus",
    "TaskNotReadyError",
    "TaskRepository",
    "TaskSource",
    "TaskSourceError",
    "TaskStateChangedError",
    "TaskStatus",
    "TaskTransition",
    "Workspace",
]
