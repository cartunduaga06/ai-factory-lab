"""Enumerations shared across the domain model.

These are deliberately plain string enums so they serialize cleanly to JSON,
GitHub labels and database columns without custom encoders.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle status of a :class:`factory.domain.models.FactoryTask`.

    See ``docs/architecture.md`` for the full state machine and the allowed
    transitions between these values.
    """

    DISCOVERED = "DISCOVERED"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    PR_OPEN = "PR_OPEN"
    WAITING_HUMAN = "WAITING_HUMAN"
    DONE = "DONE"

    # Failure / interruption paths.
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunStatus(StrEnum):
    """Status of a single :class:`factory.domain.models.AgentRun`."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class QualityGateStatus(StrEnum):
    """Outcome of a quality gate evaluated against an agent run."""

    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AgentKind(StrEnum):
    """Execution engines the factory can dispatch to.

    The orchestration layer must never branch on a concrete engine; it should
    depend only on :class:`factory.domain.models.AgentAdapter`.
    """

    OPENHANDS = "OPENHANDS"
    CODEX = "CODEX"
    OTHER = "OTHER"


class RepositoryRole(StrEnum):
    """Why the factory knows about a repository."""

    # Hosts factory tasks as issues and receives the factory's own changes.
    CONTROL_PLANE = "CONTROL_PLANE"
    # Product repository that agents operate on. Read-mostly, never force-pushed.
    TARGET = "TARGET"
