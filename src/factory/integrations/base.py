"""Base contracts for integrations.

``IssueSource`` and ``TaskRepository`` are declared in the domain (they are
ports the orchestrator depends on). This module holds the remaining contracts
that only integrations care about: the agent-engine seam and the pull-request
sink. Defining the contracts early keeps the orchestration layer decoupled from
any single provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from factory.domain.enums import AgentKind
from factory.domain.models import AgentRun, FactoryTask, PullRequest, Repository, Workspace
from factory.domain.ports import IssueSource

__all__ = ["AgentAdapterBase", "IssueSource", "PullRequestSink"]


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
