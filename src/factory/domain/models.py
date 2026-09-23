"""Typed domain models for AI Factory Lab.

These are data containers (``dataclasses``), not services. They are also the
API-boundary types of the factory: the GitHub issue graph is mapped into
:class:`FactoryTask`, agent execution into :class:`AgentRun`, and results into
:class:`PullRequest` + :class:`QualityGate`.

No business logic lives here beyond trivial, deterministic validation that
keeps the objects internally consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import uuid4

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RepositoryRole,
    RunStatus,
    TaskStatus,
)


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (never use naive datetimes)."""
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


@dataclass(slots=True, frozen=True)
class Repository:
    """A repository the factory knows about.

    ``slug`` is the canonical ``owner/name`` identifier. The factory keeps
    product repositories strictly read-mostly: it may clone and branch them,
    but never pushes to their default branch (see ``docs/security.md``).
    """

    slug: str
    role: RepositoryRole
    default_branch: str = "main"
    # Where forks/PRs should target if it differs from ``slug``.
    fork_slug: str | None = None

    def __post_init__(self) -> None:
        if "/" not in self.slug:
            raise ValueError(f"repository slug must be 'owner/name', got {self.slug!r}")


@dataclass(slots=True, frozen=True)
class Workspace:
    """An isolated checkout where a single agent run operates.

    Workspaces are ephemeral and per-run: an agent never shares a working tree
    with another run or with the factory's own repository.
    """

    workspace_id: str = field(default_factory=_new_id)
    repository_slug: str = ""
    branch: str = ""
    path: str = ""
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.branch:
            raise ValueError("workspace must be created on an isolated branch")


@dataclass(slots=True, frozen=True)
class QualityGate:
    """A single verifiable check applied to an agent run.

    Gates are named declaratively (``lint``, ``tests``, ``typecheck``, ...) so
    the orchestration layer can evaluate them uniformly regardless of the agent
    engine that produced the change.
    """

    name: str
    status: QualityGateStatus = QualityGateStatus.PENDING
    # Short human-readable detail; must never contain secrets.
    detail: str | None = None
    required: bool = True

    @property
    def is_blocking(self) -> bool:
        return self.required and self.status in {
            QualityGateStatus.PENDING,
            QualityGateStatus.FAILED,
        }


@dataclass(slots=True)
class FactoryTask:
    """A unit of work, eventually sourced from a GitHub Issue.

    The ``external_ref`` field is the link back to the originating issue
    (e.g. ``cartunduaga06/ai-factory-lab#12``). Everything else is factory
    bookkeeping: an id, the repository in scope, a lifecycle status and the
    timestamps needed for audit.
    """

    title: str
    repository_slug: str
    external_ref: str | None = None
    task_id: str = field(default_factory=_new_id)
    body: str = ""
    status: TaskStatus = TaskStatus.DISCOVERED
    labels: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("task title must not be empty")
        if "/" not in self.repository_slug:
            raise ValueError(f"repository_slug must be 'owner/name', got {self.repository_slug!r}")


@dataclass(slots=True)
class AgentRun:
    """One attempt by one agent engine to complete a task.

    A task may accumulate several runs (retries, different engines, rebases).
    ``adapter`` is the engine identifier only — the orchestration layer works
    through the :class:`AgentAdapter` protocol, never a concrete engine.
    """

    task_id: str
    adapter: AgentKind
    run_id: str = field(default_factory=_new_id)
    status: RunStatus = RunStatus.PENDING
    workspace: Workspace | None = None
    summary: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    gates: tuple[QualityGate, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }

    @property
    def all_gates_passed(self) -> bool:
        if not self.gates:
            return False
        return all(gate.status is QualityGateStatus.PASSED for gate in self.gates)


@dataclass(slots=True, frozen=True)
class PullRequest:
    """A pull request opened by an agent, awaiting mandatory human approval.

    The factory may open a PR but must never merge one. ``merged`` exists for
    bookkeeping only and is expected to be driven by an external human action.
    """

    repository_slug: str
    head_branch: str
    base_branch: str
    title: str
    number: int | None = None
    url: str | None = None
    task_id: str | None = None
    opened_at: datetime = field(default_factory=_utcnow)
    merged: bool = False


@runtime_checkable
class AgentAdapter(Protocol):
    """Structural interface every agent engine must satisfy.

    Implementations (OpenHands, Codex, future engines) translate a
    :class:`FactoryTask` into an engine-specific execution and normalize the
    result back into an :class:`AgentRun`. The orchestration layer depends only
    on this protocol, which is what keeps the factory engine-agnostic.
    """

    @property
    def kind(self) -> AgentKind:
        """Which engine this adapter drives."""
        ...

    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        """Start work on ``task`` inside ``workspace`` and return the run record."""
        ...

    def collect(self, run: AgentRun) -> AgentRun:
        """Refresh ``run`` with the engine's latest status and results."""
        ...

    def cancel(self, run: AgentRun) -> None:
        """Request cancellation of an in-flight run. Must be idempotent."""
        ...


__all__ = [
    "AgentAdapter",
    "AgentRun",
    "FactoryTask",
    "PullRequest",
    "QualityGate",
    "Repository",
    "Workspace",
]
