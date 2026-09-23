"""Base contracts for integrations.

Only abstract interfaces live here for now — concrete GitHub and agent
implementations arrive in Phase 2. Defining the contracts early keeps the
orchestration layer decoupled from any single provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from factory.domain.enums import AgentKind
from factory.domain.models import AgentRun, FactoryTask, PullRequest, Repository, Workspace


class IssueSource(ABC):
    """A system that supplies work items (currently GitHub Issues)."""

    @abstractmethod
    def list_open_tasks(self, repository: Repository) -> Sequence[FactoryTask]:
        """Return currently open work items for ``repository``."""

    @abstractmethod
    def get_task(self, repository: Repository, external_ref: str) -> FactoryTask:
        """Fetch a single task by its external reference (e.g. issue number)."""


class PullRequestSink(ABC):
    """A system that receives results (currently GitHub Pull Requests).

    Implementations may open and update pull requests. They must never merge:
    merge is a human action (see ``docs/security.md``).
    """

    @abstractmethod
    def open_pull_request(self, pull_request: PullRequest) -> PullRequest:
        """Open a pull request and return it enriched with its number and URL."""

    @abstractmethod
    def find_open_pull_request(
        self, repository: Repository, head_branch: str
    ) -> PullRequest | None:
        """Return the existing open PR for ``head_branch``, if any."""


class AgentAdapterBase(ABC):
    """Convenience base class for :class:`factory.domain.models.AgentAdapter`.

    Concrete engines subclass this instead of re-implementing the protocol.
    """

    @property
    @abstractmethod
    def kind(self) -> AgentKind:
        """Which engine this adapter drives."""

    @abstractmethod
    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        """Start work on ``task`` inside ``workspace``."""

    @abstractmethod
    def collect(self, run: AgentRun) -> AgentRun:
        """Refresh ``run`` with the engine's latest status and results."""

    @abstractmethod
    def cancel(self, run: AgentRun) -> None:
        """Request cancellation of an in-flight run. Must be idempotent."""
