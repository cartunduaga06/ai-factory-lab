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
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    Repository,
    Workspace,
)

__all__ = [
    "AgentAdapter",
    "AgentKind",
    "AgentRun",
    "FactoryTask",
    "PullRequest",
    "QualityGate",
    "QualityGateStatus",
    "Repository",
    "RepositoryRole",
    "RunStatus",
    "TaskStatus",
    "Workspace",
]
