"""Base contracts for integrations.

``IssueSource``, ``PullRequestSink`` and the other ports the orchestrator
depends on are declared in the domain, so orchestration never imports this
package. This module keeps the agent-engine convenience base class and
re-exports the port contracts for backward compatibility with earlier phases.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from factory.domain.enums import AgentKind
from factory.domain.models import AgentRun, FactoryTask, Workspace
from factory.domain.ports import IssueSource, PullRequestSink

__all__ = ["AgentAdapterBase", "IssueSource", "PullRequestSink"]


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
