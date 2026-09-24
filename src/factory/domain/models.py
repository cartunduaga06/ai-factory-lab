"""Typed domain models for AI Factory Lab.

These are data containers (``dataclasses``), not services. They are also the
API-boundary types of the factory: the GitHub issue graph is mapped into
:class:`FactoryTask`, agent execution into :class:`AgentRun`, and results into
:class:`PullRequest` + :class:`QualityGate`.

No business logic lives here beyond trivial, deterministic validation that
keeps the objects internally consistent.
"""

from __future__ import annotations

import re
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


def _validate_slug(slug: str) -> None:
    if "/" not in slug:
        raise ValueError(f"repository slug must be 'owner/name', got {slug!r}")


#: Providers are short, lowercase, ascii tokens (``github``, ``gitlab``, ...).
_PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")


@dataclass(slots=True, frozen=True)
class TaskSource:
    """Structured identity of where a task came from.

    Identity is a triple — provider + repository + issue number — not a parsed
    string. That makes it deterministic and safe to use as a uniqueness key:
    ``github`` + ``cartunduaga06/ai-factory-lab`` + ``42`` always identifies the
    same task, regardless of how it is formatted for display.

    The domain only knows the provider is a short lowercase token; which
    providers exist and how to talk to them is an integration concern.
    """

    provider: str
    repository_slug: str
    issue_number: int

    def __post_init__(self) -> None:
        if not _PROVIDER_PATTERN.fullmatch(self.provider):
            raise ValueError(
                "task source provider must be a lowercase ascii token "
                f"(letters, digits, '-', '_', '.'), got {self.provider!r}"
            )
        _validate_slug(self.repository_slug)
        if self.issue_number <= 0:
            raise ValueError(
                f"task source issue number must be positive, got {self.issue_number!r}"
            )

    @property
    def external_ref(self) -> str:
        """Human-readable reference, e.g. ``cartunduaga06/ai-factory-lab#42``.

        For display and backward compatibility only — never the source of truth
        for identity.
        """
        return f"{self.repository_slug}#{self.issue_number}"


@dataclass(slots=True, frozen=True)
class TaskTransition:
    """One audited lifecycle transition for a task.

    Recorded by persistence, not by the state machine: the machine stays a pure
    validator, and the storage layer is what makes the history durable.
    """

    task_id: str
    from_status: TaskStatus
    to_status: TaskStatus
    transition_id: str = field(default_factory=_new_id)
    occurred_at: datetime = field(default_factory=_utcnow)


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
        _validate_slug(self.slug)


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

    ``source`` is the structured, deterministic identity of the originating
    work item. ``external_ref`` is retained as a derived, human-readable
    convenience (``cartunduaga06/ai-factory-lab#12``) for display and backward
    compatibility, but it is *never* the source of identity — two different
    tasks could share a formatted string, and identity must not depend on string
    parsing.

    Everything else is factory bookkeeping: an id, the repository in scope, a
    lifecycle status and the timestamps needed for audit.
    """

    title: str
    target_repository: str
    source: TaskSource | None = None
    task_id: str = field(default_factory=_new_id)
    body: str = ""
    status: TaskStatus = TaskStatus.DISCOVERED
    labels: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("task title must not be empty")
        _validate_slug(self.target_repository)

    @property
    def external_ref(self) -> str | None:
        """Display reference derived from :attr:`source`, or ``None``."""
        return self.source.external_ref if self.source is not None else None


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
    "TaskSource",
    "TaskTransition",
    "Workspace",
]
