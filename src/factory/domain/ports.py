"""Ports (contracts) the orchestration layer depends on.

Only abstract interfaces live here — the domain defines *what* the factory
needs from the outside world, never *how* it is obtained. Concrete GitHub and
SQLite behaviour belongs in the integrations and infrastructure packages
respectively, so orchestration stays free of provider and storage details.

These interfaces are deliberately dependency-free and synchronous: the Phase 2A
intake pipeline is a manual, command-line operation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from factory.domain.enums import TaskStatus
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    QualityGate,
    QualityGateSpec,
    Repository,
    TaskSource,
    TaskTransition,
    Workspace,
)


class IssueSource(ABC):
    """A system that supplies work items (currently GitHub Issues).

    A source is read-only from the factory's perspective in Phase 2A: it lists
    and fetches work items, and never mutates them.
    """

    @abstractmethod
    def list_open_tasks(self, repository: Repository) -> Sequence[FactoryTask]:
        """Return currently open, eligible work items for ``repository``.

        Eligibility is the source's responsibility (for GitHub, an open issue
        labelled ``factory-ready`` that is not a pull request).
        """

    @abstractmethod
    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        """Fetch a single work item by its structured source identity."""


class TaskRepository(ABC):
    """Persistence boundary for tasks and their lifecycle history.

    The orchestration layer works only through this interface. It must be
    possible to implement it over SQLite, PostgreSQL or an in-memory store
    without changing a single line of orchestration code.

    Implementations must guarantee the atomicity contract documented on
    :meth:`apply_transition`.
    """

    @abstractmethod
    def initialize(self) -> None:
        """Create the schema if needed. Idempotent and safe to call repeatedly."""

    @abstractmethod
    def save(self, task: FactoryTask) -> FactoryTask:
        """Persist a new ``task`` and return it.

        Raises:
            DuplicateTaskError: if the structured source identity already
                exists and a different task holds it.
        """

    @abstractmethod
    def update(self, task: FactoryTask) -> FactoryTask:
        """Persist changes to an existing ``task`` and return it.

        Raises:
            KeyError: if no task with ``task.task_id`` is stored.
        """

    @abstractmethod
    def get(self, task_id: str) -> FactoryTask | None:
        """Return the task with ``task_id``, or ``None`` if it is unknown."""

    @abstractmethod
    def find_by_source(self, source: TaskSource) -> FactoryTask | None:
        """Return the task identified by ``source``, or ``None``.

        This is the deterministic lookup that makes intake idempotent.
        """

    @abstractmethod
    def list(self, status: TaskStatus | None = None) -> Sequence[FactoryTask]:
        """Return stored tasks, optionally filtered by ``status``.

        Ordering is stable (by creation time, then id) so callers can rely on it.
        """

    @abstractmethod
    def apply_transition(
        self, task_id: str, expected_from: TaskStatus, target: TaskStatus
    ) -> FactoryTask:
        """Atomically move ``task_id`` from ``expected_from`` to ``target``.

        Implementations must perform, in a single atomic operation:

        1. load the current task,
        2. confirm its stored status is ``expected_from``,
        3. update the task status, and
        4. append a transition-history record.

        If any step fails, nothing is committed. ``expected_from`` is a
        compare-and-swap guard: it turns a lost update into an explicit error
        instead of silently applying a transition to an unexpected state.

        Transition *legality* is validated by the orchestration layer before
        this call; this method is the durable, atomic write.

        Raises:
            KeyError: if the task is unknown.
            TaskStateChangedError: if the stored status is not ``expected_from``.
        """

    @abstractmethod
    def history(self, task_id: str) -> Sequence[TaskTransition]:
        """Return the transition history for ``task_id``, oldest first."""


class RunRepository(ABC):
    """Persistence boundary for workspaces and agent runs.

    Dispatch creates durable records for the isolated workspace a run operates in
    and for the run itself. As with :class:`TaskRepository`, orchestration sees
    only this interface, so the storage engine stays an implementation detail.

    Implementations must guarantee the atomicity contract documented on
    :meth:`save_run`.
    """

    @abstractmethod
    def initialize(self) -> None:
        """Create the schema if needed. Idempotent and safe to call repeatedly."""

    @abstractmethod
    def save_workspace(self, workspace: Workspace) -> Workspace:
        """Persist a new ``workspace`` and return it.

        Raises:
            DuplicateRunError: if a workspace with the same ``workspace_id``
                already exists.
        """

    @abstractmethod
    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Return the workspace with ``workspace_id``, or ``None`` if unknown."""

    @abstractmethod
    def save_run(self, run: AgentRun) -> AgentRun:
        """Persist a new ``run`` and return it.

        Persisting a run also persists its workspace when one is attached, in a
        single atomic operation, so a run is never stored without the workspace it
        claims to operate in.

        Raises:
            DuplicateRunError: if a run with the same ``run_id`` exists, or if the
                task already has an active (non-terminal) run.
        """

    @abstractmethod
    def get_run(self, run_id: str) -> AgentRun | None:
        """Return the run with ``run_id``, or ``None`` if unknown."""

    @abstractmethod
    def update_run(self, run: AgentRun) -> AgentRun:
        """Persist changes to an existing ``run`` and return it.

        This is *not* an upsert: it updates a run that is already stored and
        never creates one. ``run_id`` and task identity are immutable — the
        stored row is matched by ``run_id`` and its ``task_id`` must not change.
        Every mutable field is written: ``status``, ``summary``, ``started_at``,
        ``finished_at``, the run's workspace reference and ``gates``.

        The write is refused, rather than silently inserting, when the run is
        unknown. It does not create a second run, and the one-active-run-per-task
        invariant is unaffected because no new row is added.

        Raises:
            KeyError: if no run with ``run.run_id`` is stored.
            PersistenceError: if the stored run belongs to a different task.
        """

    @abstractmethod
    def list_runs(self, task_id: str | None = None) -> Sequence[AgentRun]:
        """Return stored runs, optionally filtered to one task, oldest first."""

    @abstractmethod
    def find_active_run(self, task_id: str) -> AgentRun | None:
        """Return the task's active run, or ``None``.

        This is the deterministic lookup that makes dispatch idempotent: a task
        that already has a run in a non-terminal status has been dispatched and
        must not be dispatched again.
        """


class WorkspaceProvisioner(ABC):
    """Prepares the physical, isolated working tree a run operates in.

    The orchestration layer depends only on this contract. How the checkout is
    materialised — a Git worktree, a fresh clone, a container volume — is an
    implementation detail that belongs outside the domain and orchestration.

    Implementations must be **retry-safe**: preparing a workspace that already
    exists and already points at the expected branch is a no-op that returns the
    same workspace. Preparing a *different* workspace must yield a different
    branch and path. An existing checkout that does not match the requested
    workspace must be rejected rather than silently reused.
    """

    @abstractmethod
    def prepare(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        """Create (or confirm) ``workspace`` for ``task`` and return it.

        Raises:
            WorkspaceProvisioningError: if the workspace cannot be prepared. The
                error is sanitized — it never carries raw process output, a
                command line or a credential.
        """


class QualityGateRunner(ABC):
    """Executes a declarative quality gate inside a run's workspace.

    Implementations own process execution and therefore all of its safety
    requirements (no shell, bounded timeout, sanitized result). The orchestration
    layer only asks "did this gate pass?" and never builds a command itself.
    """

    @abstractmethod
    def run(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        """Evaluate ``spec`` with the workspace as the working directory.

        Returns a :class:`~factory.domain.models.QualityGate` whose ``detail`` is
        sanitized and never contains raw process output or a credential.
        """


__all__ = [
    "IssueSource",
    "QualityGateRunner",
    "RunRepository",
    "TaskRepository",
    "WorkspaceProvisioner",
]
