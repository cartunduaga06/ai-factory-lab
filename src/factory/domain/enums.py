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


class ValidationOutcome(StrEnum):
    """Deterministic result of validating a succeeded agent run.

    The factory stops at the validation decision in Phase 4: it never advances
    the task to ``PR_OPEN``, because no pull request exists yet. ``GATES_FAILED``
    is the signal later orchestration uses to decide whether corrective work is
    required — Phase 4 does not dispatch a correction itself.
    """

    # The agent run has not reached a terminal success yet.
    PENDING = "PENDING"
    # Every configured required gate passed; the run is ready for the next phase.
    READY_FOR_NEXT_PHASE = "READY_FOR_NEXT_PHASE"
    # At least one required gate did not pass. The task stays VALIDATING.
    GATES_FAILED = "GATES_FAILED"


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
