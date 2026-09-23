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
from factory.domain.models import FactoryTask, Repository, TaskSource, TaskTransition


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


__all__ = ["IssueSource", "TaskRepository"]
