"""A deterministic in-memory :class:`AgentAdapter` for tests.

Phase 2B has no real engine integration, so dispatch is exercised against this
double. It satisfies the protocol structurally — it is not a mock of the code
under test, it is a real, self-contained adapter implementation.
"""

from __future__ import annotations

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentRun, FactoryTask, Workspace


class FakeAgentAdapter:
    """Records dispatch calls and returns a predictable run."""

    def __init__(
        self,
        *,
        kind: AgentKind = AgentKind.OTHER,
        status: RunStatus = RunStatus.PENDING,
        summary: str = "fake run",
        fail_with: Exception | None = None,
    ) -> None:
        self._kind = kind
        self._status = status
        self._summary = summary
        self._fail_with = fail_with
        self.dispatched: list[tuple[str, str]] = []
        self.collected = 0
        self.cancelled: list[str] = []

    @property
    def kind(self) -> AgentKind:
        return self._kind

    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        self.dispatched.append((task.task_id, workspace.workspace_id))
        if self._fail_with is not None:
            raise self._fail_with
        return AgentRun(
            task_id=task.task_id,
            adapter=self._kind,
            status=self._status,
            workspace=workspace,
            summary=self._summary,
        )

    def collect(self, run: AgentRun) -> AgentRun:
        self.collected += 1
        return run

    def cancel(self, run: AgentRun) -> None:
        self.cancelled.append(run.run_id)


__all__ = ["FakeAgentAdapter"]
