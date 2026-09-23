"""AI Factory Lab — orchestration control plane for autonomous coding agents.

The factory is intentionally independent from the product repositories it
operates on. It coordinates agents (OpenHands, Codex, ...) against external
repositories, but never owns their application code.

Layering (outer layers may depend on inner ones, never the reverse):

* ``factory.domain``          — pure typed model, no I/O.
* ``factory.orchestration``   — task lifecycle and agent dispatch.
* ``factory.integrations``    — adapters to external systems (GitHub, agents).
* ``factory.infrastructure``  — configuration, logging, persistence.
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
from factory.orchestration.lifecycle import TRANSITIONS, can_transition, next_states

__version__ = "0.1.0"

__all__ = [
    "TRANSITIONS",
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
    "__version__",
    "can_transition",
    "next_states",
]
